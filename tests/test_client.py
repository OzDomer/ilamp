"""
Tests for clap/client.py against a fake hub.

The client is one more WebSocket client of the hub: it keeps the lamp's
state from pushed messages and, on a double clap, sends the one request
that toggles the lamp. The fake hub below answers like the real one.
"""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from clap.client import HubClient

ON = {"on": True, "brightness": 180, "rgb": [255, 120, 40], "mode": "normal"}
OFF = {**ON, "on": False}
SUN_ON = {"on": True, "temperature": 128, "level": 16}
SUN_OFF = {**SUN_ON, "on": False}


class FakeSocket:
    """What `async with connect(url) as ws` yields: iterable for messages, send() for requests."""

    def __init__(self, hub):
        self.hub = hub
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()

    def push(self, message: dict) -> None:
        self.incoming.put_nowait(json.dumps(message))

    def close(self) -> None:
        self.incoming.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        text = await self.incoming.get()
        if text is None:
            raise ConnectionError("closed")
        return text

    async def send(self, text: str) -> None:
        self.hub.received.append(json.loads(text))
        self.hub.answer(self, json.loads(text))


class FakeHub:
    def __init__(self, lamp=ON, sun=SUN_OFF, fail_first: int = 0):
        self.lamp, self.sun = dict(lamp), dict(sun)
        self.received: list[dict] = []
        self.sockets: list[FakeSocket] = []
        self.fail_first = fail_first
        self.attempts = 0

    @asynccontextmanager
    async def connect(self, url: str):
        self.attempts += 1
        if self.attempts <= self.fail_first:
            raise OSError("connection refused")
        socket = FakeSocket(self)
        self.sockets.append(socket)
        socket.push(self.state())
        yield socket

    def state(self) -> dict:
        return {"type": "state", "connected": True, "lamp": self.lamp, "sun": self.sun}

    def answer(self, socket: FakeSocket, request: dict) -> None:
        # the real hub: the lamp runs one light at a time
        match request["type"]:
            case "power":
                self.lamp["on"] = request["on"]
                if request["on"]:
                    self.sun["on"] = False
            case "sun":
                self.sun["on"] = request["on"]
                if request["on"]:
                    self.lamp["on"] = False
        socket.push({"type": "ack", "id": request["id"]})
        socket.push(self.state())


def run(coro):
    return asyncio.run(coro)


async def running(client: HubClient):
    task = asyncio.create_task(client.run())
    await asyncio.sleep(0.05)
    return task


async def stop(task: asyncio.Task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_knows_the_lamp_state_after_connecting():
    async def go():
        hub = FakeHub()
        client = HubClient("ws://fake/ws", connect=hub.connect, retry_delays=(0.01,))
        task = await running(client)
        try:
            assert client.connected is True
            assert client.lamp_on is True and client.sun_on is False
        finally:
            await stop(task)

    run(go())


def test_toggle_turns_the_lit_light_off():
    async def go():
        hub = FakeHub(lamp=ON)
        client = HubClient("ws://fake/ws", connect=hub.connect, retry_delays=(0.01,))
        task = await running(client)
        try:
            await client.toggle()
            assert hub.received[-1]["type"] == "power" and hub.received[-1]["on"] is False
            await asyncio.sleep(0.01)
            assert client.lamp_on is False
        finally:
            await stop(task)

    run(go())


def test_toggle_turns_the_ring_off_when_that_is_what_is_on():
    async def go():
        hub = FakeHub(lamp=OFF, sun=SUN_ON)
        client = HubClient("ws://fake/ws", connect=hub.connect, retry_delays=(0.01,))
        task = await running(client)
        try:
            await client.toggle()
            assert hub.received[-1] == {"id": 1, "type": "sun", "on": False}
        finally:
            await stop(task)

    run(go())


def test_toggle_from_all_off_brings_back_the_last_light():
    async def go():
        hub = FakeHub(lamp=OFF, sun=SUN_ON)
        client = HubClient("ws://fake/ws", connect=hub.connect, retry_delays=(0.01,))
        task = await running(client)
        try:
            await client.toggle()  # ring off: everything is off now
            await asyncio.sleep(0.01)
            await client.toggle()  # back on: the ring, since it was on last
            assert hub.received[-1] == {"id": 2, "type": "sun", "on": True}
        finally:
            await stop(task)

    run(go())


def test_toggle_while_disconnected_is_refused_not_hung():
    async def go():
        hub = FakeHub(fail_first=1000)
        client = HubClient("ws://fake/ws", connect=hub.connect, retry_delays=(0.01,))
        task = await running(client)
        try:
            assert client.connected is False
            with pytest.raises(ConnectionError):
                await asyncio.wait_for(client.toggle(), 0.5)
        finally:
            await stop(task)

    run(go())


def test_reconnects_after_the_hub_goes_away():
    async def go():
        hub = FakeHub(fail_first=2)
        client = HubClient("ws://fake/ws", connect=hub.connect, retry_delays=(0.01,))
        task = await running(client)
        try:
            assert client.connected is True and hub.attempts == 3
            hub.sockets[-1].close()  # the hub restarts
            await asyncio.sleep(0.05)
            assert client.connected is True and hub.attempts == 4
        finally:
            await stop(task)

    run(go())
