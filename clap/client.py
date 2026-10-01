"""
client.py - one more client of the hub.

The hub is the only thing that talks to the lamp (docs/adr/0001). This
client connects to it over the WebSocket like the web page does, keeps
the lamp's state from the pushed "state" messages, and on toggle() sends
the one request that turns the lamp off, or back on. It reconnects on its
own if the hub goes away, forever.

The connection is injected (`connect`) so tests can plug in a fake hub.
"""

import asyncio
import json
import logging
from collections.abc import Sequence
from typing import Any

import websockets

log = logging.getLogger(__name__)

RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)
REQUEST_TIMEOUT = 5.0


class HubClient:
    def __init__(
        self,
        url: str,
        connect: Any = websockets.connect,
        retry_delays: Sequence[float] = RETRY_DELAYS,
    ):
        self.url = url
        self._connect = connect
        self._delays = tuple(retry_delays)
        self._ws: Any = None
        self.lamp_on: bool | None = None  # None until the hub has told us
        self.sun_on: bool | None = None
        self._last_light = "rgb"  # which light to bring back from all-off
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}

    @property
    def connected(self) -> bool:
        return self._ws is not None and self.lamp_on is not None

    # -- the connection -----------------------------------------------------

    async def run(self) -> None:
        """Stay connected to the hub for as long as this runs. Never returns."""
        failures = 0
        while True:
            try:
                async with self._connect(self.url) as ws:
                    self._ws = ws
                    failures = 0
                    log.info("Connected to the hub at %s", self.url)
                    async for text in ws:
                        self._handle(json.loads(text))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - whatever happened, we reconnect
                log.warning("Hub connection problem (%s).", e)
            finally:
                self._ws = None
                self.lamp_on = self.sun_on = None
                self._fail_pending("lost the hub connection")
            delay = self._delays[min(failures, len(self._delays) - 1)]
            failures += 1
            log.info("Reconnecting to the hub in %ss.", delay)
            await asyncio.sleep(delay)

    def _handle(self, message: dict) -> None:
        match message.get("type"):
            case "state":
                lamp, sun = message.get("lamp"), message.get("sun")
                self.lamp_on = None if lamp is None else bool(lamp["on"])
                self.sun_on = None if sun is None else bool(sun["on"])
                if self.sun_on:
                    self._last_light = "sun"
                elif self.lamp_on:
                    self._last_light = "rgb"
            case "ack":
                future = self._pending.pop(message.get("id"), None)
                if future is not None and not future.done():
                    future.set_result(None)
            case "error":
                future = self._pending.pop(message.get("id"), None)
                if future is not None and not future.done():
                    future.set_exception(RuntimeError(message.get("message", "hub error")))
                else:
                    log.warning("Hub error: %s", message.get("message"))

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError(reason))
        self._pending.clear()

    # -- the one thing a clap does ---------------------------------------------

    async def toggle(self) -> None:
        """
        Whichever light is on, off it goes. From all-off, bring back the
        light that was on last. Raises ConnectionError when the hub is away.
        """
        if not self.connected:
            raise ConnectionError("not connected to the hub")
        if self.sun_on:
            request: dict = {"type": "sun", "on": False}
        elif self.lamp_on:
            request = {"type": "power", "on": False}
        elif self._last_light == "sun":
            request = {"type": "sun", "on": True}
        else:
            request = {"type": "power", "on": True}
        await self._request(request)

    async def _request(self, body: dict) -> None:
        """Send one request; return when the hub acks it, raise on its error."""
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._ws.send(json.dumps({"id": request_id, **body}))
            await asyncio.wait_for(future, REQUEST_TIMEOUT)
        finally:
            self._pending.pop(request_id, None)
