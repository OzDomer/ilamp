"""
Helpers for tests that need a LampService talking to the fake lamp.
"""

import asyncio

from hub.service import HubStatus, LampService
from ilamp import Lamp, LampNotFoundError
from tests.fake_lamp import FakeLamp, factory_for

FAST = {"heartbeat_interval": 0.05, "confirm_timeout": 0.3}


def make_service(fail_first: int = 0, **fake_options):
    """
    A service whose Lamp talks to a FakeLamp. fail_first=N makes the first
    N connection attempts fail with LampNotFoundError (a missed scan).
    """
    fakes: list[FakeLamp] = []
    attempts = {"count": 0}
    real_factory = factory_for(fakes, **fake_options)

    async def client_factory(name, on_disconnect):
        attempts["count"] += 1
        if attempts["count"] <= fail_first:
            raise LampNotFoundError("no lamp this time")
        return await real_factory(name, on_disconnect)

    def lamp_factory(**kwargs) -> Lamp:
        return Lamp(client_factory=client_factory, **FAST, **kwargs)

    service = LampService(lamp_factory=lamp_factory, retry_delays=(0.05, 0.05))
    return service, fakes, attempts


async def next_status(queue: asyncio.Queue, timeout: float = 1.0) -> HubStatus:
    return await asyncio.wait_for(queue.get(), timeout)


async def wait_until(predicate, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def run(coro):
    return asyncio.run(coro)
