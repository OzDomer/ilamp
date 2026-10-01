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
"""

import asyncio
from collections.abc import Callable

from . import protocol
from .protocol import LampState, Mode
from .session import Session

CONFIRM_TIMEOUT = 2.0  # seconds to wait for the lamp to report a change


class CommandNotConfirmedError(Exception):
    """The lamp didn't report the expected state in time (rejected or lost)."""


class Lamp:
    def __init__(
        self,
        name: str = "i_Lamp",
        color_scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
        confirm_timeout: float = CONFIRM_TIMEOUT,
        on_disconnect: Callable[[], None] | None = None,
        **session_options,
    ):
        """
        color_scale: multiply R, G, B by these before sending. The lamp's
            green and blue overpower red, so pure white looks cyan; something
            like (1.0, 0.7, 0.6) can balance it. Tune by eye.
        session_options: passed through to Session (tests use this to plug
            in the fake lamp).
        """
        self._scale = color_scale
        self._timeout = confirm_timeout
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
        self._waiters: list[tuple[Callable[[LampState], bool], asyncio.Future]] = []

    # -- lifecycle ------------------------------------------------------------

    async def connect(self) -> None:
        await self._session.open()
        await self.refresh()

    async def disconnect(self) -> None:
        await self._session.close()

    async def __aenter__(self) -> "Lamp":
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
        waiter = self._wait_for(lambda s: True)
        await self._session.send(protocol.read_state())
        return await self._confirm(waiter, "read state")

    # -- commands -------------------------------------------------------------

    async def on(self) -> LampState:
        return await self._command(protocol.power(True), lambda s: s.on, "power on")

    async def off(self) -> LampState:
        return await self._command(protocol.power(False), lambda s: not s.on, "power off")

    async def color(self, r: int, g: int, b: int, brightness: int | None = None) -> LampState:
        """
        Set the color, and optionally brightness (0-255). Without a
        brightness, the current one is kept - the lamp always takes both
        together, so we fill in the missing one from lamp.state.
        """
        if brightness is None:
            brightness = self._state.brightness if self._state else 255
        rgb = self._apply_scale(r, g, b)
        return await self._command(
            protocol.color(*rgb, brightness=brightness),
            lambda s: s.rgb == rgb and s.brightness == brightness,
            f"color {rgb} brightness {brightness}",
        )

    async def brightness(self, value: int) -> LampState:
        """Change brightness only, keeping the current color."""
        r, g, b = self._state.rgb if self._state else (255, 255, 255)
        return await self._command(
            protocol.color(r, g, b, brightness=value),
            lambda s: s.brightness == value,
            f"brightness {value}",
        )

    async def mode(self, m: Mode | int) -> LampState:
        """Switch light mode. Unknown values are rejected by the lamp -> error."""
        return await self._command(protocol.mode(m), lambda s: s.mode == m, f"mode {m!r}")

    # -- internals ------------------------------------------------------------

    def _apply_scale(self, r: int, g: int, b: int) -> tuple[int, int, int]:
        return tuple(min(255, round(v * k)) for v, k in zip((r, g, b), self._scale))

    async def _command(
        self, packet: bytes, done: Callable[[LampState], bool], label: str
    ) -> LampState:
        """
        Send a command and wait until the lamp confirms it.

        The lamp only reports CHANGES. If the known state already satisfies
        the command, no report will come - so we send it (harmless) and
        return right away instead of waiting for nothing.
        """
        if self._state is not None and done(self._state):
            await self._session.send(packet)
            return self._state
        waiter = self._wait_for(done)
        await self._session.send(packet)
        return await self._confirm(waiter, label)

    def _wait_for(self, condition: Callable[[LampState], bool]) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        self._waiters.append((condition, future))
        return future

    async def _confirm(self, future: asyncio.Future, label: str) -> LampState:
        try:
            return await asyncio.wait_for(future, self._timeout)
        except asyncio.TimeoutError:
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
        for condition, future in self._waiters:
            if not future.done() and condition(state):
                future.set_result(state)
