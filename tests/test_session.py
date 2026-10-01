"""
Tests for session.py, run against the fake lamp.

Each test checks one behavior we proved on the real lamp, so the session
keeps doing what worked even after future changes.

(Tests are plain functions calling asyncio.run(), so no extra pytest
plugin is needed for async code.)
"""

import asyncio

import pytest

from ilamp import protocol as p
from ilamp.session import NotConnectedError, Session
from tests.fake_lamp import FakeLamp, factory_for

FAST = 0.05  # heartbeat interval for tests (the fake's watchdog is 0.3s)


def make_session(**kwargs):
    lamps: list[FakeLamp] = []
    session = Session(client_factory=factory_for(lamps), heartbeat_interval=FAST, **kwargs)
    return session, lamps


def test_opens_with_hello_then_handshake():
    async def run():
        session, lamps = make_session()
        async with session:
            pass
        sent = lamps[0].received
        assert sent[0] == p.HELLO
        assert sent[1:3] == p.handshake()

    asyncio.run(run())


def test_heartbeat_keeps_lamp_connected_past_watchdog():
    async def run():
        session, lamps = make_session()
        async with session:
            await asyncio.sleep(1.0)  # > 3x the fake's watchdog
            assert session.is_connected
            assert lamps[0].heartbeats >= 10

    asyncio.run(run())


def test_fake_lamp_drops_without_heartbeat():
    # Sanity check of the fake itself: if it didn't enforce the watchdog,
    # the test above would prove nothing.
    async def run():
        lamp = FakeLamp(watchdog=0.3)
        await lamp.connect()
        await asyncio.sleep(0.6)
        assert not lamp.is_connected

    asyncio.run(run())


def test_command_reaches_lamp_and_reply_comes_back():
    async def run():
        received = []
        session, lamps = make_session(on_packet=received.append)
        async with session:
            await session.send(p.power(False))
            await asyncio.sleep(0.1)
        assert lamps[0].state["power"] == p.Power.OFF
        states = [s for s in map(p.parse_state, received) if s is not None]
        assert states and states[-1].on is False

    asyncio.run(run())


def test_unexpected_drop_is_reported():
    async def run():
        drops = []
        session, lamps = make_session(on_disconnect=lambda: drops.append(1))
        async with session:
            # Simulate the lamp vanishing (unplugged, out of range...).
            lamps[0]._drop(report=True)
            await asyncio.sleep(0.1)
            with pytest.raises(NotConnectedError):
                await session.send(p.power(True))
        assert drops == [1]

    asyncio.run(run())


def test_normal_close_is_not_reported_as_a_drop():
    async def run():
        drops = []
        session, lamps = make_session(on_disconnect=lambda: drops.append(1))
        async with session:
            pass
        assert drops == []
        assert not lamps[0].is_connected

    asyncio.run(run())


def test_heartbeat_stops_after_close():
    async def run():
        session, lamps = make_session()
        async with session:
            await asyncio.sleep(0.2)
        count = lamps[0].heartbeats
        await asyncio.sleep(0.2)
        assert lamps[0].heartbeats == count

    asyncio.run(run())
