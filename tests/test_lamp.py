"""
Tests for lamp.py, run against the fake lamp.

These check the behavior a USER relies on: commands are confirmed, the
state stays current, and bad requests fail loudly instead of silently.
"""

import asyncio

import pytest

from ilamp import CommandNotConfirmedError, Lamp, Mode, SunState
from tests.fake_lamp import FakeLamp, factory_for


def make_lamp(fake_options: dict | None = None, **kwargs):
    lamps: list[FakeLamp] = []
    options = {"heartbeat_interval": 0.05, "confirm_timeout": 0.3, **kwargs}
    lamp = Lamp(client_factory=factory_for(lamps, **(fake_options or {})), **options)
    return lamp, lamps


def run(coro):
    return asyncio.run(coro)


def test_state_is_known_right_after_connecting():
    async def go():
        lamp, _ = make_lamp()
        async with lamp:
            # The fake starts as: on, white, full brightness, normal mode.
            assert lamp.state.on is True
            assert lamp.state.rgb == (255, 255, 255)
            assert lamp.state.mode == Mode.NORMAL

    run(go())


def test_power_off_and_on_are_confirmed():
    async def go():
        lamp, fakes = make_lamp()
        async with lamp:
            state = await lamp.off()
            assert state.on is False and fakes[0].state["power"] == 2
            state = await lamp.on()
            assert state.on is True

    run(go())


def test_color_keeps_current_brightness():
    async def go():
        lamp, _ = make_lamp()
        async with lamp:
            await lamp.brightness(40)
            state = await lamp.color(255, 0, 0)
            assert state.rgb == (255, 0, 0)
            assert state.brightness == 40

    run(go())


def test_brightness_keeps_current_color():
    async def go():
        lamp, _ = make_lamp()
        async with lamp:
            await lamp.color(0, 0, 255)
            state = await lamp.brightness(20)
            assert state.rgb == (0, 0, 255) and state.brightness == 20

    run(go())


def test_mode_is_confirmed():
    async def go():
        lamp, _ = make_lamp()
        async with lamp:
            state = await lamp.mode(Mode.RAINBOW)
            assert state.mode == Mode.RAINBOW

    run(go())


def test_rejected_mode_raises_instead_of_failing_silently():
    async def go():
        lamp, _ = make_lamp()
        async with lamp:
            with pytest.raises(CommandNotConfirmedError):
                await lamp.mode(0x02)  # the real lamp ignores 02

    run(go())


def test_command_that_changes_nothing_returns_immediately():
    # The lamp only reports changes. Turning on a lamp that's already on
    # gets no report - we must not sit there waiting for one.
    async def go():
        lamp, _ = make_lamp(confirm_timeout=5.0)
        async with lamp:
            start = asyncio.get_running_loop().time()
            await lamp.on()  # already on
            assert asyncio.get_running_loop().time() - start < 1.0

    run(go())


def test_color_scale_is_applied():
    async def go():
        lamp, fakes = make_lamp(color_scale=(1.0, 0.5, 0.25))
        async with lamp:
            state = await lamp.color(200, 200, 200)
            assert state.rgb == (200, 100, 50)
            assert fakes[0].state["rgb"] == [200, 100, 50]

    run(go())


def test_failed_connect_tears_down_the_session():
    # Python skips __aexit__ when __aenter__ raises. So if refresh() fails
    # inside connect(), connect() itself must close the session, or the
    # heartbeat task and the Bluetooth link are left running with no owner.
    async def go():
        lamp, fakes = make_lamp(fake_options={"answer_reads": False})
        with pytest.raises(CommandNotConfirmedError):
            async with lamp:
                pass
        fake = fakes[0]
        assert not fake.is_connected
        beats = fake.heartbeats
        await asyncio.sleep(0.2)
        assert fake.heartbeats == beats  # the heartbeat task is really gone

    run(go())


def test_concurrent_commands_do_not_clobber_each_other():
    # color() keeps the current brightness and brightness() keeps the
    # current color, both read from lamp.state. If they run at the same
    # time, both read the OLD state before either is confirmed, and the
    # second command sends the first one's old value back.
    async def go():
        lamp, _ = make_lamp()
        async with lamp:
            await asyncio.gather(lamp.color(255, 0, 0), lamp.brightness(80))
            assert lamp.state.rgb == (255, 0, 0)
            assert lamp.state.brightness == 80

    run(go())


def test_on_state_hears_every_report_including_unrequested_ones():
    # The hub must learn about changes it didn't ask for (another input,
    # the lamp's own buttons), not just the ones it is waiting on.
    async def go():
        seen = []
        lamp, fakes = make_lamp(on_state=seen.append)
        async with lamp:
            await lamp.color(255, 0, 0)
            fakes[0].external_change(power=2)  # someone pressed the lamp's button
            await asyncio.sleep(0.05)
        assert [(s.on, s.rgb) for s in seen] == [
            (True, (255, 255, 255)),  # the refresh() on connect
            (True, (255, 0, 0)),  # our command
            (False, (255, 0, 0)),  # the button press
        ]

    run(go())


# -- the sun ring -----------------------------------------------------------------


def test_sun_state_is_known_right_after_connecting():
    async def go():
        lamp, _ = make_lamp()
        async with lamp:
            # The fake starts with the ring off, level 16, temperature 128.
            assert lamp.sun == SunState(on=False, level=16, temperature=128)

    run(go())


def test_sun_on_is_confirmed_and_implies_the_rgb_light_went_off():
    # The lamp switches the RGB light off when the ring comes on, but does
    # NOT report that. Lamp must infer it, and tell on_state about it.
    async def go():
        seen = []
        lamp, fakes = make_lamp(on_state=seen.append)
        async with lamp:
            assert lamp.state.on is True
            sun = await lamp.sun_on()
            assert sun.on is True and fakes[0].sun["power"] == 1
            assert fakes[0].state["power"] == 2  # the fake copies the real side effect
            assert lamp.state.on is False  # inferred, since the lamp doesn't say
            assert seen[-1].on is False

    run(go())


def test_rgb_on_is_confirmed_and_implies_the_ring_went_off():
    async def go():
        seen = []
        lamp, fakes = make_lamp(on_sun=seen.append)
        async with lamp:
            await lamp.sun_on()
            await lamp.on()
            assert lamp.state.on is True
            assert lamp.sun.on is False and fakes[0].sun["power"] == 2
            assert seen[-1].on is False

    run(go())


def test_sun_temperature_is_confirmed():
    async def go():
        lamp, fakes = make_lamp()
        async with lamp:
            sun = await lamp.sun_temperature(0)  # warm
            assert sun.temperature == 0 and fakes[0].sun["temperature"] == 0
            with pytest.raises(ValueError):
                await lamp.sun_temperature(300)

    run(go())


def test_sun_off_is_confirmed():
    async def go():
        lamp, fakes = make_lamp()
        async with lamp:
            await lamp.sun_on()
            sun = await lamp.sun_off()
            assert sun.on is False and fakes[0].sun["power"] == 2

    run(go())
