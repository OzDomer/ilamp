"""
Tests for lamp.py, run against the fake lamp.

These check the behavior a USER relies on: commands are confirmed, the
state stays current, and bad requests fail loudly instead of silently.
"""

import asyncio

import pytest

from ilamp import CommandNotConfirmedError, Lamp, Mode
from tests.fake_lamp import FakeLamp, factory_for


def make_lamp(**kwargs):
    lamps: list[FakeLamp] = []
    options = {"heartbeat_interval": 0.05, "confirm_timeout": 0.3, **kwargs}
    lamp = Lamp(client_factory=factory_for(lamps), **options)
    return lamp, lamps


def run(coro):
    return asyncio.run(coro)


def test_state_is_known_right_after_connecting():
    async def go():
        lamp, fakes = make_lamp()
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
        lamp, fakes = make_lamp()
        async with lamp:
            await lamp.brightness(40)
            state = await lamp.color(255, 0, 0)
            assert state.rgb == (255, 0, 0)
            assert state.brightness == 40
    run(go())


def test_brightness_keeps_current_color():
    async def go():
        lamp, fakes = make_lamp()
        async with lamp:
            await lamp.color(0, 0, 255)
            state = await lamp.brightness(20)
            assert state.rgb == (0, 0, 255) and state.brightness == 20
    run(go())


def test_mode_is_confirmed():
    async def go():
        lamp, fakes = make_lamp()
        async with lamp:
            state = await lamp.mode(Mode.RAINBOW)
            assert state.mode == Mode.RAINBOW
    run(go())


def test_rejected_mode_raises_instead_of_failing_silently():
    async def go():
        lamp, fakes = make_lamp()
        async with lamp:
            with pytest.raises(CommandNotConfirmedError):
                await lamp.mode(0x02)  # the real lamp ignores 02
    run(go())


def test_command_that_changes_nothing_returns_immediately():
    # The lamp only reports changes. Turning on a lamp that's already on
    # gets no report - we must not sit there waiting for one.
    async def go():
        lamp, fakes = make_lamp(confirm_timeout=5.0)
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
