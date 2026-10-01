"""
service.py - the one owner of the lamp.

The lamp takes a single Bluetooth controller at a time, so exactly one
object in the whole system holds the connection: a LampService. Everything
else (web UI, clap detector, voice) is a client of this service and never
touches `ilamp` directly. See docs/adr/0001-server-owns-the-lamp.md.

What the service does:
  - connects on start() and keeps reconnecting, forever, with a backoff
  - knows the lamp's last reported state (`status`)
  - tells every subscriber whenever that status changes, including changes
    nobody here asked for (the lamp's own buttons, another controller)
  - fails fast while the lamp is away, instead of timing out

No web code in this file. It is tested against the fake lamp, and it is
what the projector HUD will reuse later.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

from ilamp import Lamp, LampState, Mode

log = logging.getLogger(__name__)

# Seconds between connection attempts. The last value repeats. A missed
# scan is normal for this lamp, so we never give up; we just slow down.
RETRY_DELAYS = (2.0, 4.0, 8.0, 15.0)


class LampUnavailableError(Exception):
    """The hub is not connected to the lamp right now (it keeps trying)."""


@dataclass(frozen=True)
class HubStatus:
    """What the hub knows: is the lamp connected, and what did it last report."""

    connected: bool
    lamp: LampState | None  # None while disconnected


# Builds a Lamp. The service passes on_state= and on_disconnect= itself;
# tests pass a factory that plugs in the fake lamp.
LampFactory = Callable[..., Lamp]


class LampService:
    def __init__(
        self,
        lamp_factory: LampFactory = Lamp,
        retry_delays: Sequence[float] = RETRY_DELAYS,
    ):
        self._make_lamp = lamp_factory
        self._delays = tuple(retry_delays)
        self._lamp: Lamp | None = None  # the CURRENT lamp; old ones are ignored
        self._status = HubStatus(connected=False, lamp=None)
        self._subscribers: set[asyncio.Queue[HubStatus]] = set()
        self._supervisor: asyncio.Task | None = None
        self._dropped = asyncio.Event()  # set when the current lamp hangs up

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Start connecting in the background. Returns immediately."""
        self._supervisor = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        """Stop reconnecting and hang up. Safe to call more than once."""
        if self._supervisor is not None:
            self._supervisor.cancel()
            await asyncio.gather(self._supervisor, return_exceptions=True)
            self._supervisor = None
        await self._drop_lamp()
        self._set_status(HubStatus(connected=False, lamp=None))

    @property
    def status(self) -> HubStatus:
        return self._status

    # -- subscriptions --------------------------------------------------------

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[HubStatus]]:
        """
        A queue that receives every status change for as long as the
        `async with` block runs. The current status is queued first, so a
        client that joins late learns the state right away.
        """
        queue: asyncio.Queue[HubStatus] = asyncio.Queue()
        queue.put_nowait(self._status)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    # -- commands -------------------------------------------------------------

    async def power(self, on: bool) -> LampState:
        lamp = self._require_lamp()
        return await (lamp.on() if on else lamp.off())

    async def color(self, r: int, g: int, b: int, brightness: int | None = None) -> LampState:
        return await self._require_lamp().color(r, g, b, brightness)

    async def brightness(self, value: int) -> LampState:
        return await self._require_lamp().brightness(value)

    async def mode(self, m: Mode) -> LampState:
        return await self._require_lamp().mode(m)

    async def refresh(self) -> LampState:
        return await self._require_lamp().refresh()

    def _require_lamp(self) -> Lamp:
        if self._lamp is None or not self._status.connected:
            raise LampUnavailableError("The lamp is not connected right now; the hub keeps trying.")
        return self._lamp

    # -- the supervisor -------------------------------------------------------

    async def _supervise(self) -> None:
        """Connect, stay connected, reconnect. Runs for the service's whole life."""
        failures = 0
        while True:
            self._dropped.clear()
            lamp = self._new_lamp()
            try:
                await lamp.connect()
            except Exception as e:  # noqa: BLE001 - whatever the reason, we retry
                delay = self._delays[min(failures, len(self._delays) - 1)]
                failures += 1
                log.warning("Connecting to the lamp failed (%s). Retrying in %ss.", e, delay)
                await asyncio.sleep(delay)
                continue

            failures = 0
            self._lamp = lamp
            self._set_status(HubStatus(connected=True, lamp=lamp.state))
            log.info("Connected to the lamp: %s", lamp.state)

            await self._dropped.wait()
            log.warning("The lamp dropped the connection. Reconnecting.")
            await self._drop_lamp()
            self._set_status(HubStatus(connected=False, lamp=None))
            await asyncio.sleep(self._delays[0])

    def _new_lamp(self) -> Lamp:
        """
        Build a lamp whose callbacks know which lamp they belong to. Only the
        current lamp may change the status; a late report or drop from an
        old one (after a reconnect) is ignored.
        """
        holder: list[Lamp] = []

        def on_state(state: LampState) -> None:
            if holder and holder[0] is self._lamp:
                self._set_status(HubStatus(connected=True, lamp=state))

        def on_disconnect() -> None:
            if holder and holder[0] is self._lamp:
                self._dropped.set()

        lamp = self._make_lamp(on_state=on_state, on_disconnect=on_disconnect)
        holder.append(lamp)
        return lamp

    async def _drop_lamp(self) -> None:
        lamp, self._lamp = self._lamp, None
        if lamp is not None:
            try:
                await lamp.disconnect()
            except Exception as e:  # noqa: BLE001 - it's already gone; nothing to do
                log.debug("Ignoring error while hanging up: %s", e)

    def _set_status(self, status: HubStatus) -> None:
        if status == self._status:
            return  # nothing changed; don't wake every client for nothing
        self._status = status
        for queue in self._subscribers:
            queue.put_nowait(status)
