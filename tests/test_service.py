"""
Tests for hub/service.py, run against the fake lamp.

The service is the one thing that owns the lamp. These tests pin down what
every client can rely on: the hub connects on its own, tells subscribers
what the lamp reported, fails fast while the lamp is away, and comes back
by itself when the lamp does.
"""

import asyncio

import pytest

from hub.service import LampUnavailableError
from ilamp import Mode
from tests.service_helpers import make_service, next_status, run, wait_until


def test_start_connects_and_knows_the_lamp_state():
    async def go():
        service, fakes, _ = make_service()
        await service.start()
        try:
            await wait_until(lambda: service.status.connected)
            assert service.status.lamp.rgb == (255, 255, 255)
            assert fakes[0].is_connected
        finally:
            await service.stop()

    run(go())


def test_stop_disconnects_and_stops_reconnecting():
    async def go():
        service, fakes, attempts = make_service()
        await service.start()
        await wait_until(lambda: service.status.connected)
        await service.stop()
        assert not fakes[0].is_connected
        assert service.status.connected is False
        await asyncio.sleep(0.2)  # several retry delays
        assert attempts["count"] == 1  # no reconnect attempts after stop

    run(go())


def test_subscribers_get_the_current_status_first():
    # A client that connects late must not wait for the next change to
    # learn what the lamp is doing right now.
    async def go():
        service, _, _ = make_service()
        await service.start()
        try:
            await wait_until(lambda: service.status.connected)
            async with service.subscribe() as queue:
                status = await next_status(queue)
                assert status.connected is True
                assert status.lamp.on is True
        finally:
            await service.stop()

    run(go())


def test_commands_reach_the_lamp_and_every_subscriber_hears_the_result():
    async def go():
        service, fakes, _ = make_service()
        await service.start()
        try:
            await wait_until(lambda: service.status.connected)
            async with service.subscribe() as a, service.subscribe() as b:
                await next_status(a), await next_status(b)  # the initial status
                state = await service.color(255, 0, 0)
                assert state.rgb == (255, 0, 0)
                assert fakes[0].state["rgb"] == [255, 0, 0]
                for queue in (a, b):
                    status = await next_status(queue)
                    assert status.lamp.rgb == (255, 0, 0)
                await service.mode(Mode.RAINBOW)
                assert (await next_status(a)).lamp.mode == Mode.RAINBOW
        finally:
            await service.stop()

    run(go())


def test_changes_the_hub_did_not_ask_for_are_broadcast_too():
    async def go():
        service, fakes, _ = make_service()
        await service.start()
        try:
            await wait_until(lambda: service.status.connected)
            async with service.subscribe() as queue:
                await next_status(queue)
                fakes[0].external_change(power=2)  # the lamp's own button
                status = await next_status(queue)
                assert status.lamp.on is False
                assert service.status.lamp.on is False
        finally:
            await service.stop()

    run(go())


def test_leaving_the_subscription_stops_delivery():
    async def go():
        service, fakes, _ = make_service()
        await service.start()
        try:
            await wait_until(lambda: service.status.connected)
            async with service.subscribe() as queue:
                await next_status(queue)
            fakes[0].external_change(power=2)
            await asyncio.sleep(0.05)
            assert queue.empty()
        finally:
            await service.stop()

    run(go())


def test_commands_fail_fast_while_the_lamp_is_away():
    async def go():
        service, _, _ = make_service(fail_first=1000)  # never found
        await service.start()
        try:
            await asyncio.sleep(0.1)
            assert service.status.connected is False
            start = asyncio.get_running_loop().time()
            with pytest.raises(LampUnavailableError):
                await service.power(True)
            assert asyncio.get_running_loop().time() - start < 0.1
        finally:
            await service.stop()

    run(go())


def test_keeps_retrying_until_the_lamp_is_found():
    # A missed scan is normal for this lamp. Not a reason to give up.
    async def go():
        service, _, attempts = make_service(fail_first=2)
        await service.start()
        try:
            await wait_until(lambda: service.status.connected)
            assert attempts["count"] == 3
        finally:
            await service.stop()

    run(go())


def test_reconnects_after_the_lamp_vanishes():
    async def go():
        service, fakes, _ = make_service()
        await service.start()
        try:
            await wait_until(lambda: service.status.connected)
            async with service.subscribe() as queue:
                await next_status(queue)
                fakes[0].vanish()
                status = await next_status(queue)
                assert status.connected is False and status.lamp is None
                status = await next_status(queue)
                assert status.connected is True
                assert len(fakes) == 2 and fakes[1].is_connected
                # and it works again
                await service.power(False)
                assert fakes[1].state["power"] == 2
        finally:
            await service.stop()

    run(go())


def test_a_stale_lamp_cannot_change_the_status_after_a_reconnect():
    # After a reconnect, a late report from the OLD connection must be
    # ignored, or it would overwrite what the live lamp is saying.
    async def go():
        service, fakes, _ = make_service()
        await service.start()
        try:
            await wait_until(lambda: service.status.connected)
            fakes[0].vanish()
            await wait_until(lambda: len(fakes) == 2 and service.status.connected)
            await service.color(0, 0, 255)
            fakes[0].external_change(rgb=[9, 9, 9])  # ghost of the old connection
            await asyncio.sleep(0.05)
            assert service.status.lamp.rgb == (0, 0, 255)
        finally:
            await service.stop()

    run(go())
