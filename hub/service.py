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
from dataclasses import dataclass, replace

from ilamp import Lamp, LampState, Mode, SunState

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
    lamp: LampState | None  # the RGB light; None while disconnected
    sun: SunState | None = None  # the white ring; None while disconnected


# Builds a Lamp. The service passes on_state= and on_disconnect= itself;
# tests pass a factory that plugs in the fake lamp.
LampFactory = Callable[..., Lamp]


class LampService:
    def __init__(
        self,
        lamp_factory: LampFactory = Lamp,
        retry_delays: Sequence[float] = RETRY_DELAYS,
        color_scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
    ):
        """
        color_scale: R, G, B multipliers applied before a color reaches the
            lamp (its green and blue overpower red, so white looks cyan).
            The lamp reports the corrected bytes back; the service undoes
            the scale on the way in, so clients always see the color they
            asked for and the correction stays a hardware detail.
        """
        self._make_lamp = lamp_factory
        self._delays = tuple(retry_delays)
        self._scale = color_scale
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

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

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
        return self._perceived(await (lamp.on() if on else lamp.off()))

    async def color(self, r: int, g: int, b: int, brightness: int | None = None) -> LampState:
        return self._perceived(await self._require_lamp().color(r, g, b, brightness))

    async def brightness(self, value: int) -> LampState:
        return self._perceived(await self._require_lamp().brightness(value))

    async def mode(self, m: Mode) -> LampState:
        return self._perceived(await self._require_lamp().mode(m))

    async def refresh(self) -> LampState:
        return self._perceived(await self._require_lamp().refresh())

    async def sun(self, on: bool) -> SunState:
        lamp = self._require_lamp()
        return await (lamp.sun_on() if on else lamp.sun_off())

    async def sun_temperature(self, value: int) -> SunState:
        return await self._require_lamp().sun_temperature(value)

    async def sun_level(self, value: int) -> SunState:
        return await self._require_lamp().sun_level(value)

    def _perceived(self, state: LampState) -> LampState:
        """The lamp's state with the color scale undone: what the user asked for."""
        rgb = tuple(
            min(255, round(v / k)) if k > 0 else v
            for v, k in zip(state.rgb, self._scale, strict=True)
        )
        return replace(state, rgb=rgb)

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
            self._set_status(
                HubStatus(connected=True, lamp=self._perceived(lamp.state), sun=lamp.sun)
            )
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
                self._set_status(replace(self._status, connected=True, lamp=self._perceived(state)))

        def on_sun(sun: SunState) -> None:
            if holder and holder[0] is self._lamp:
                self._set_status(replace(self._status, connected=True, sun=sun))

        def on_disconnect() -> None:
            if holder and holder[0] is self._lamp:
                self._dropped.set()

        lamp = self._make_lamp(
            on_state=on_state,
            on_sun=on_sun,
            on_disconnect=on_disconnect,
            color_scale=self._scale,
        )
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
