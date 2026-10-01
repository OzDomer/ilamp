"""
server.py - the hub's web face.

Transport only. One WebSocket endpoint (/ws) where each client:
  - receives the hub's status as soon as it connects, and again on every change
  - sends requests (see messages.py) and gets an ack or an error for each

Everything lamp-related lives in service.py. This file parses, validates,
hands over, and replies. create_app() takes the service as an argument so
tests can pass one that talks to the fake lamp.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from ilamp import CommandNotConfirmedError

from . import messages
from .messages import (
    BrightnessRequest,
    ColorRequest,
    ModeRequest,
    PowerRequest,
    RefreshRequest,
    Request,
    SunRequest,
    SunTemperatureRequest,
)
from .service import HubStatus, LampService, LampUnavailableError

log = logging.getLogger(__name__)

Send = Callable[[dict], Awaitable[None]]

STATIC_DIR = Path(__file__).parent / "static"  # the web UI: index.html, app.js, style.css


def create_app(service: LampService) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # The service lives exactly as long as the server does.
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(title="ilamp hub", lifespan=lifespan)

    @app.websocket("/ws")
    async def websocket(ws: WebSocket) -> None:
        await ws.accept()
        # Two tasks talk to one socket (status pushes and request replies);
        # the lock keeps their messages from interleaving.
        send_lock = asyncio.Lock()

        async def send(message: dict) -> None:
            async with send_lock:
                await ws.send_json(message)

        async with service.subscribe() as updates:
            pusher = asyncio.create_task(_push_updates(updates, send))
            try:
                await _handle_requests(ws, service, send)
            except WebSocketDisconnect:
                pass  # the client left; normal
            finally:
                pusher.cancel()
                await asyncio.gather(pusher, return_exceptions=True)

    # Mounted last, so /ws is matched first: everything else is the UI.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")

    return app


async def _push_updates(updates: asyncio.Queue[HubStatus], send: Send) -> None:
    """Forward every status change to this client. Cancelled when the client leaves."""
    while True:
        status = await updates.get()
        await send(messages.status_message(status))


async def _handle_requests(ws: WebSocket, service: LampService, send: Send) -> None:
    """Read requests until the client disconnects. One ack or error per request."""
    while True:
        text = await ws.receive_text()
        try:
            request = messages.parse_request(text)
        except ValueError as e:
            await send(messages.error_message(str(e), messages.peek_id(text)))
            continue
        try:
            await _dispatch(service, request)
        except (LampUnavailableError, CommandNotConfirmedError) as e:
            await send(messages.error_message(str(e), request.id))
        else:
            await send(messages.ack_message(request.id))


async def _dispatch(service: LampService, request: Request) -> None:
    match request:
        case PowerRequest():
            await service.power(request.on)
        case ColorRequest():
            await service.color(request.r, request.g, request.b, request.brightness)
        case BrightnessRequest():
            await service.brightness(request.value)
        case ModeRequest():
            await service.mode(request.mode)
        case RefreshRequest():
            await service.refresh()
        case SunRequest():
            await service.sun(request.on)
        case SunTemperatureRequest():
            await service.sun_temperature(request.value)
