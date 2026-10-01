"""
lamp.py - the friendly front door.

    async with Lamp() as lamp:
        await lamp.color(255, 0, 0)
        await lamp.mode(Mode.RAINBOW)
        print(lamp.state)

Built on the two layers below:
  protocol.py  turns intentions into bytes and replies into LampState
  session.py   moves those bytes over Bluetooth and keeps the lamp awake

What this layer adds:
  - CONFIRMED commands: each call waits until the lamp reports the new
    state, and raises if it doesn't. No silent failures.
  - It knows the lamp only reports CHANGES, so asking for the current
    state returns immediately instead of waiting forever.
  - lamp.state is always the lamp's latest self-reported state.
  - ONE command at a time. Commands that fill in half their value from
    lamp.state (color keeps brightness, brightness keeps color) would
    otherwise read a stale state when run concurrently.
"""

import asyncio
from collections.abc import Callable
from typing import Self

from . import protocol
from .protocol import LampState, Mode
from .session import Session

CONFIRM_TIMEOUT = 2.0  # seconds to wait for the lamp to report a change

# A plan turns the CURRENT state into (packet to send, "is it done yet?" test).
# It's a function, not a value, so _command can call it inside the lock.
Done = Callable[[LampState], bool]
Plan = Callable[[LampState | None], tuple[bytes, Done]]


class CommandNotConfirmedError(Exception):
    """The lamp didn't report the expected state in time (rejected or lost)."""


class Lamp:
    def __init__(
        self,
        name: str = "i_Lamp",
        color_scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
        confirm_timeout: float = CONFIRM_TIMEOUT,
        on_disconnect: Callable[[], None] | None = None,
        on_state: Callable[[LampState], None] | None = None,
        **session_options,
    ):
        """
        color_scale: multiply R, G, B by these before sending. The lamp's
            green and blue overpower red, so pure white looks cyan; something
            like (1.0, 0.7, 0.6) can balance it. Tune by eye.
        on_state: called with every state the lamp reports, including
            changes nobody here asked for (another controller, the lamp's
            own buttons). Must not raise and must not block.
        session_options: passed through to Session (tests use this to plug
            in the fake lamp).
        """
        self._scale = color_scale
        self._timeout = confirm_timeout
        self._on_state = on_state
        self._session = Session(
            name=name,
            on_packet=self._handle_packet,
            on_disconnect=on_disconnect,
            **session_options,
        )
        self._state: LampState | None = None
        # Commands waiting for confirmation: (condition, future) pairs.
        # When a state report arrives, every waiter whose condition is now
        # true gets resolved.
        self._waiters: list[tuple[Done, asyncio.Future]] = []
        # One exchange with the lamp at a time: read state -> build -> send
        # -> confirmed. Without this, two concurrent commands both read the
        # old state and the second one sends the first one's old value back.
        self._command_lock = asyncio.Lock()

    # -- lifecycle ------------------------------------------------------------

    async def connect(self) -> None:
        await self._session.open()
        try:
            await self.refresh()
        except BaseException:
            # The session is open and heartbeating, but connect() failed.
            # `async with` won't call __aexit__ for us, so close it here.
            await self._session.close()
            raise

    async def disconnect(self) -> None:
        await self._session.close()

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._session.is_connected

    # -- state ----------------------------------------------------------------

    @property
    def state(self) -> LampState | None:
        """The lamp's most recently reported state (None before connecting)."""
        return self._state

    async def refresh(self) -> LampState:
        """Ask the lamp for its full state and wait for the answer."""
        async with self._command_lock:
            waiter = self._wait_for(lambda s: True)
            await self._session.send(protocol.read_state())
            return await self._confirm(waiter, "read state")

    # -- commands -------------------------------------------------------------

    async def on(self) -> LampState:
        return await self._command(self._fixed(protocol.power(True), lambda s: s.on), "power on")

    async def off(self) -> LampState:
        return await self._command(
            self._fixed(protocol.power(False), lambda s: not s.on), "power off"
        )

    async def color(self, r: int, g: int, b: int, brightness: int | None = None) -> LampState:
        """
        Set the color, and optionally brightness (0-255). Without a
        brightness, the current one is kept - the lamp always takes both
        together, so we fill in the missing one from the current state.
        """
        rgb = self._apply_scale(r, g, b)

        def plan(state: LampState | None) -> tuple[bytes, Done]:
            keep = state.brightness if state else 255
            wanted = keep if brightness is None else brightness
            packet = protocol.color(*rgb, brightness=wanted)
            return packet, lambda s: s.rgb == rgb and s.brightness == wanted

        return await self._command(plan, f"color {rgb}")

    async def brightness(self, value: int) -> LampState:
        """Change brightness only, keeping the current color."""

        def plan(state: LampState | None) -> tuple[bytes, Done]:
            r, g, b = state.rgb if state else (255, 255, 255)
            return protocol.color(r, g, b, brightness=value), lambda s: s.brightness == value

        return await self._command(plan, f"brightness {value}")

    async def mode(self, m: Mode | int) -> LampState:
        """Switch light mode. Unknown values are rejected by the lamp -> error."""
        return await self._command(
            self._fixed(protocol.mode(m), lambda s: s.mode == m), f"mode {m!r}"
        )

    # -- internals ------------------------------------------------------------

    def _apply_scale(self, r: int, g: int, b: int) -> tuple[int, int, int]:
        return tuple(min(255, round(v * k)) for v, k in zip((r, g, b), self._scale))

    @staticmethod
    def _fixed(packet: bytes, done: Done) -> Plan:
        """A plan for commands that don't depend on the current state."""
        return lambda _state: (packet, done)

    async def _command(self, plan: Plan, label: str) -> LampState:
        """
        Build a command from the current state, send it, and wait until the
        lamp confirms it. The whole thing happens under the command lock, so
        the state the plan sees is the confirmed result of the previous one.

        The lamp only reports CHANGES. If the known state already satisfies
        the command, no report will come - so we send it (harmless) and
        return right away instead of waiting for nothing.
        """
        async with self._command_lock:
            packet, done = plan(self._state)
            if self._state is not None and done(self._state):
                await self._session.send(packet)
                return self._state
            waiter = self._wait_for(done)
            await self._session.send(packet)
            return await self._confirm(waiter, label)

    def _wait_for(self, condition: Done) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        self._waiters.append((condition, future))
        return future

    async def _confirm(self, future: asyncio.Future, label: str) -> LampState:
        try:
            return await asyncio.wait_for(future, self._timeout)
        except TimeoutError:
            raise CommandNotConfirmedError(
                f"The lamp didn't confirm '{label}' within {self._timeout}s "
                "(rejected, or the connection is struggling)."
            ) from None
        finally:
            self._waiters = [(c, f) for c, f in self._waiters if f is not future]

    def _handle_packet(self, data: bytes) -> None:
        """Called by the session for every packet the lamp sends."""
        state = protocol.parse_state(data)
        if state is None:
            return  # heartbeat answers and other replies
        self._state = state
        if self._on_state is not None:
            self._on_state(state)
        for condition, future in self._waiters:
            if not future.done() and condition(state):
                future.set_result(state)
