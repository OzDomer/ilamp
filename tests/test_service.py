"""
Tests for hub/service.py, run against the fake lamp.

The service is the one thing that owns the lamp. These tests pin down what
every client can rely on: the hub connects on its own, tells subscribers
what the lamp reported, fails fast while the lamp is away, and comes back
by itself when the lamp does.
"""

import asyncio
import logging

import pytest

from hub.service import LampService, LampUnavailableError
from ilamp import CommandNotConfirmedError, Lamp, Mode
from tests.fake_lamp import factory_for
from tests.service_helpers import FAST, make_service, next_status, run, wait_until


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


def test_reported_colors_are_in_the_users_space_not_the_lamps():
    # color_scale corrects the lamp's cyan-leaning white on the way OUT.
    # The lamp then reports the corrected bytes back. Clients must see the
    # color they asked for, or "white" would render orange on the page.
    async def go():
        service, fakes, _ = make_service(color_scale=(1.0, 0.5, 0.25))
        await service.start()
        try:
            await wait_until(lambda: service.status.connected)
            state = await service.color(200, 200, 200)
            assert fakes[0].state["rgb"] == [200, 100, 50]  # what the lamp got
            assert state.rgb == (200, 200, 200)  # what the user asked for
            assert service.status.lamp.rgb == (200, 200, 200)
        finally:
            await service.stop()

    run(go())


def test_the_ring_is_part_of_the_status_and_its_changes_are_broadcast():
    async def go():
        service, fakes, _ = make_service()
        await service.start()
        try:
            await wait_until(lambda: service.status.connected)
            assert service.status.sun.on is False
            async with service.subscribe() as queue:
                await next_status(queue)
                sun = await service.sun(True)
                assert sun.on is True and fakes[0].sun["power"] == 1
                # the ring coming on switched the RGB light off: both land in the status
                status = await next_status(queue)
                while status.lamp.on or not status.sun.on:
                    status = await next_status(queue)
                assert status.lamp.on is False and status.sun.on is True
                await service.sun_temperature(0)
                status = await next_status(queue)
                assert status.sun.temperature == 0
        finally:
            await service.stop()

    run(go())


def test_ring_level_goes_through_the_service():
    async def go():
        service, _, _ = make_service()
        await service.start()
        try:
            await wait_until(lambda: service.status.connected)
            sun = await service.sun_level(8)
            assert sun.level == 8 and service.status.sun.level == 8
        finally:
            await service.stop()

    run(go())


def test_a_drop_while_a_command_waits_for_the_lock_is_reported_as_unavailable():
    # _require_lamp() passes before the command queues up behind the lock.
    # If the lamp drops meanwhile, the queued command must still fail with
    # the service's own error, not with a session-layer NotConnectedError
    # that nothing above knows how to answer.
    async def go():
        service, fakes, _ = make_service()
        await service.start()
        try:
            await wait_until(lambda: service.status.connected)
            first = asyncio.create_task(
                service.mode(2)
            )  # rejected: holds the lock until it times out
            await asyncio.sleep(0.05)
            second = asyncio.create_task(service.power(False))  # queued behind the lock
            await asyncio.sleep(0.05)
            fakes[0].vanish()
            with pytest.raises(CommandNotConfirmedError):
                await first
            with pytest.raises(LampUnavailableError):
                await second
        finally:
            await service.stop()

    run(go())


def test_the_supervisor_survives_an_unexpected_error_and_logs_it(caplog):
    # Anything unexpected inside the supervisor (here: the lamp factory
    # itself blowing up once) must not kill it silently. It logs and retries.
    attempts = {"count": 0}

    def flaky_lamp_factory(**kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise TypeError("bad factory")
        return Lamp(client_factory=factory_for([]), **FAST, **kwargs)

    async def go():
        service = LampService(lamp_factory=flaky_lamp_factory, retry_delays=(0.05,))
        await service.start()
        try:
            with caplog.at_level(logging.WARNING, logger="hub.service"):
                await wait_until(lambda: service.status.connected)
            assert attempts["count"] == 2
            assert any("bad factory" in r.getMessage() for r in caplog.records)
        finally:
            await service.stop()

    run(go())
