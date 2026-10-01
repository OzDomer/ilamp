"""
session.py - the Bluetooth plumbing for talking to the lamp.

This layer knows how to MOVE packets, not what they mean:
  - find the lamp and connect
  - subscribe to its replies
  - open a session (hello + handshake)
  - keep it alive with a heartbeat in the background
  - close everything down cleanly

It knows nothing about colors or modes - that's protocol.py's job (what the
bytes mean) and lamp.py's job (what you want to do). Porting to another
platform, like an ESP32, means rewriting only this file.

Dependency injection
--------------------
The session doesn't create its Bluetooth client directly. It asks a
"client factory" for one. In real use that's bleak (see bleak_factory). In
tests it's a fake lamp, so the session can be tested without hardware.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from . import protocol

# Phone -> lamp: every command, heartbeat and handshake goes here.
WRITE_UUID = "00008877-0000-1000-8000-00805f9b34fb"
# Lamp -> phone: replies arrive as notifications here.
NOTIFY_UUID = "00008888-0000-1000-8000-00805f9b34fb"

DEFAULT_NAME = "i_Lamp"
HEARTBEAT_INTERVAL = 0.5  # seconds; what the app used. The lamp drops us after ~14s without.


class Client(Protocol):
    """
    The small slice of a BLE client the session actually uses.

    bleak's BleakClient has all of these. A fake lamp only needs these too.
    (A typing.Protocol just describes a shape; nothing has to inherit it.)
    """

    @property
    def is_connected(self) -> bool: ...
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def start_notify(self, uuid: str, callback: Callable) -> None: ...
    async def stop_notify(self, uuid: str) -> None: ...
    async def write_gatt_char(self, uuid: str, data: bytes, response: bool = False) -> None: ...


# A factory gets the device name and a "you got disconnected" callback,
# and returns a client that is ready to connect().
ClientFactory = Callable[[str, Callable[[], None]], Awaitable[Client]]


async def bleak_factory(name: str, on_disconnect: Callable[[], None]) -> Client:
    """The real factory: scan for the lamp by name and wrap it in a BleakClient."""
    from bleak import BleakClient, BleakScanner  # only needed for real hardware

    device = await BleakScanner.find_device_by_name(name, timeout=10)
    if device is None:
        raise LampNotFoundError(
            f"No device named {name!r} found. Is the lamp on, and is nothing "
            "else (like the phone app) connected to it?"
        )
    # bleak passes the client to the callback; we don't need it.
    return BleakClient(device, timeout=20, disconnected_callback=lambda _c: on_disconnect())


class LampNotFoundError(Exception):
    pass


class NotConnectedError(Exception):
    pass


class Session:
    """
    An open, kept-alive connection to the lamp.

    Use it as an async context manager so it always closes properly:

        async with Session(on_packet=print) as session:
            await session.send(protocol.power(True))
    """

    def __init__(
        self,
        name: str = DEFAULT_NAME,
        on_packet: Callable[[bytes], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
        client_factory: ClientFactory = bleak_factory,
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
    ):
        self.name = name
        self.on_packet = on_packet          # called with every packet the lamp sends
        self.on_disconnect = on_disconnect  # called if the lamp drops us
        self._factory = client_factory
        self._interval = heartbeat_interval
        self._client: Client | None = None
        self._heartbeat_task: asyncio.Task | None = None
        # Two things write to the lamp at the same time: the heartbeat task
        # and whoever sends commands. The lock makes them take turns, so two
        # packets never get interleaved mid-write.
        self._write_lock = asyncio.Lock()
        self._closing = False

    # -- lifecycle ----------------------------------------------------------

    async def open(self) -> None:
        """Connect, subscribe, say hello, handshake, start the heartbeat."""
        self._client = await self._factory(self.name, self._handle_disconnect)
        await self._client.connect()
        # Subscribe BEFORE sending anything, so no reply is missed.
        await self._client.start_notify(NOTIFY_UUID, self._handle_notification)

        # The exact opening the app uses. Order matters: a command sent
        # before this gets us kicked off within half a second.
        await self._write(protocol.HELLO)
        for packet in protocol.handshake():
            await self._write(packet)

        # From here on, the heartbeat keeps the lamp from hanging up.
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def close(self) -> None:
        """Stop the heartbeat and disconnect. Safe to call more than once."""
        self._closing = True
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            # Wait until the task has really stopped (and swallow the
            # CancelledError it raises) before touching the connection.
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None
        if self._client is not None and self._client.is_connected:
            try:
                await self._client.stop_notify(NOTIFY_UUID)
            finally:
                await self._client.disconnect()

    async def __aenter__(self) -> "Session":
        await self.open()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    # -- sending ------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def send(self, packet: bytes) -> None:
        """Send one packet (usually built with protocol.py)."""
        if not self.is_connected:
            raise NotConnectedError("The lamp is not connected.")
        await self._write(packet)

    async def _write(self, data: bytes) -> None:
        async with self._write_lock:
            # response=False -> "Write Command", fire and forget, like the app.
            await self._client.write_gatt_char(WRITE_UUID, data, response=False)

    # -- background work ----------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Runs in the background for the whole session, like setInterval in JS."""
        beat = protocol.heartbeat()
        try:
            while self.is_connected:
                await self._write(beat)
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            raise  # close() cancelled us - that's the normal way to stop
        except Exception:
            return  # the connection died mid-write; _handle_disconnect reports it

    # -- callbacks from the Bluetooth client ---------------------------------

    def _handle_notification(self, _sender, data: bytearray) -> None:
        if self.on_packet is not None:
            self.on_packet(bytes(data))

    def _handle_disconnect(self) -> None:
        # Only report drops we didn't ask for.
        if not self._closing and self.on_disconnect is not None:
            self.on_disconnect()
