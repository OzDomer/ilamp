"""
lamp.py - the friendly front door.

    async with Lamp() as lamp:
        await lamp.color(255, 0, 0)
        await lamp.mode(Mode.RAINBOW)
        await lamp.sun_on()
        print(lamp.state, lamp.sun)

Built on the two layers below:
  protocol.py  turns intentions into bytes and replies into states
  session.py   moves those bytes over Bluetooth and keeps the lamp awake

What this layer adds:
  - CONFIRMED commands: each call waits until the lamp reports the new
    state, and raises if it doesn't. No silent failures.
  - It knows the lamp only reports CHANGES, so asking for the current
    state returns immediately instead of waiting forever.
  - lamp.state (the RGB light) and lamp.sun (the white ring) are always
    the lamp's latest self-reported state.
  - ONE command at a time. Commands that fill in half their value from
    lamp.state (color keeps brightness, brightness keeps color) would
    otherwise read a stale state when run concurrently.
  - The two lights never run together: the lamp switches one off when the
    other comes on, but never reports that. This layer infers it.
"""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from typing import Self

from . import protocol
from .protocol import LampState, Mode, SunState
from .session import Session

CONFIRM_TIMEOUT = 2.0  # seconds to wait for the lamp to report a change

# A plan turns the CURRENT state into (packet to send, "is it done yet?" test).
# It's a function, not a value, so _command can call it inside the lock.
Done = Callable[[LampState], bool]
Plan = Callable[[LampState | None], tuple[bytes, Done]]
SunDone = Callable[[SunState], bool]


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
        on_sun: Callable[[SunState], None] | None = None,
        **session_options,
    ):
        """
        color_scale: multiply R, G, B by these before sending. The lamp's
            green and blue overpower red, so pure white looks cyan; something
            like (1.0, 0.7, 0.6) can balance it. Tune by eye.
        on_state / on_sun: called with every state the lamp reports for the
            RGB light / the ring, including changes nobody here asked for
            (another controller, the lamp's own buttons). Must not raise
            and must not block.
        session_options: passed through to Session (tests use this to plug
            in the fake lamp).
        """
        self._scale = color_scale
        self._timeout = confirm_timeout
        self._on_state = on_state
        self._on_sun = on_sun
        self._session = Session(
            name=name,
            on_packet=self._handle_packet,
            on_disconnect=on_disconnect,
            **session_options,
        )
        self._state: LampState | None = None
        self._sun: SunState | None = None
        # Commands waiting for confirmation: (condition, future) pairs, one
        # list per light. When a report arrives, every waiter whose
        # condition is now true gets resolved.
        self._light_waiters: list[tuple[Done, asyncio.Future]] = []
        self._sun_waiters: list[tuple[SunDone, asyncio.Future]] = []
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
        """The RGB light's most recently reported state (None before connecting)."""
        return self._state

    @property
    def sun(self) -> SunState | None:
        """The white ring's most recently reported state (None before connecting)."""
        return self._sun

    async def refresh(self) -> LampState:
        """
        Ask the lamp for everything and wait for the answers: the RGB
        state, then the ring's power and level, then its temperature (the
        lamp keeps those in separate messages).
        """
        async with self._command_lock:
            state = await self._exchange(
                protocol.read_state(), self._light_waiters, lambda s: True, "read state"
            )
            await self._exchange(
                protocol.read_sun_state(), self._sun_waiters, lambda s: True, "read sun state"
            )
            await self._exchange(
                protocol.read_sun_temperature(),
                self._sun_waiters,
                lambda s: s.temperature is not None,
                "read sun temperature",
            )
            return state

    # -- the RGB light ----------------------------------------------------------

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

    # -- the white ring ---------------------------------------------------------

    async def sun_on(self) -> SunState:
        """Turn the ring on. The lamp switches the RGB light off by itself."""
        return await self._sun_command(protocol.sun_power(True), lambda s: s.on, "sun on")

    async def sun_off(self) -> SunState:
        return await self._sun_command(protocol.sun_power(False), lambda s: not s.on, "sun off")

    async def sun_temperature(self, value: int) -> SunState:
        """The ring's color temperature: 0 = warm (yellowish) .. 255 = cold white."""
        packet = protocol.sun_temperature(value)  # range-checked here, before the lock
        return await self._sun_command(
            packet, lambda s: s.temperature == value, f"sun temperature {value}"
        )

    # -- internals ------------------------------------------------------------

    def _apply_scale(self, r: int, g: int, b: int) -> tuple[int, int, int]:
        return tuple(min(255, round(v * k)) for v, k in zip((r, g, b), self._scale, strict=True))

    @staticmethod
    def _fixed(packet: bytes, done: Done) -> Plan:
        """A plan for commands that don't depend on the current state."""
        return lambda _state: (packet, done)

    async def _command(self, plan: Plan, label: str) -> LampState:
        """
        Build an RGB command from the current state, send it, and wait until
        the lamp confirms it. The whole thing happens under the command lock,
        so the state the plan sees is the confirmed result of the previous one.
        """
        async with self._command_lock:
            packet, done = plan(self._state)
            return await self._exchange(packet, self._light_waiters, done, label, self._state)

    async def _sun_command(self, packet: bytes, done: SunDone, label: str) -> SunState:
        async with self._command_lock:
            return await self._exchange(packet, self._sun_waiters, done, label, self._sun)

    async def _exchange(self, packet, waiters, done, label, current=None):
        """
        Send a packet and wait for a report that satisfies `done`.

        The lamp only reports CHANGES. If the known state already satisfies
        the command, no report will come - so we send it (harmless) and
        return right away instead of waiting for nothing.
        """
        if current is not None and done(current):
            await self._session.send(packet)
            return current
        future = asyncio.get_running_loop().create_future()
        waiters.append((done, future))
        try:
            await self._session.send(packet)
            return await asyncio.wait_for(future, self._timeout)
        except TimeoutError:
            raise CommandNotConfirmedError(
                f"The lamp didn't confirm '{label}' within {self._timeout}s "
                "(rejected, or the connection is struggling)."
            ) from None
        finally:
            waiters[:] = [(c, f) for c, f in waiters if f is not future]

    def _handle_packet(self, data: bytes) -> None:
        """Called by the session for every packet the lamp sends."""
        state = protocol.parse_state(data)
        if state is not None:
            self._set_state(state)
            if state.on and self._sun is not None and self._sun.on:
                # The lamp never runs both lights, and never reports the loser.
                self._set_sun(replace(self._sun, on=False))
            return
        update = protocol.parse_sun(data)
        if update is not None:
            before = self._sun or SunState(on=False, level=0, temperature=None)
            self._set_sun(before.apply(update))
            if update.on and self._state is not None and self._state.on:
                self._set_state(replace(self._state, on=False))

    def _set_state(self, state: LampState) -> None:
        self._state = state
        if self._on_state is not None:
            self._on_state(state)
        self._resolve(self._light_waiters, state)

    def _set_sun(self, sun: SunState) -> None:
        self._sun = sun
        if self._on_sun is not None:
            self._on_sun(sun)
        self._resolve(self._sun_waiters, sun)

    @staticmethod
    def _resolve(waiters, value) -> None:
        for condition, future in waiters:
            if not future.done() and condition(value):
                future.set_result(value)
