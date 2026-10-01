# 0002: FastAPI for the hub

Status: accepted, 2026-10-01

## Context

The hub (see [0001](0001-server-owns-the-lamp.md)) needs an async Python web server: one WebSocket endpoint carrying JSON, static files for a plain HTML/JS UI, and startup/shutdown hooks to own the `Lamp`. It must run on the same asyncio loop as `bleak`. Two candidates were compared; both have Python 3.14 Windows wheels.

## Decision

FastAPI, run with uvicorn, with Pydantic models for message validation.

## Alternatives considered

- **aiohttp.** One package covering both the server and a WebSocket client (useful for the Python-side inputs later), fewer layers, and a style closer to the explicit code in `ilamp/`. It was the recommendation on technical fit. FastAPI was chosen for its much larger ecosystem and learning value: it is the framework most likely to be met again.
- **A Python desktop GUI toolkit instead of a web UI.** Rejected earlier: they fight asyncio, and a browser UI works from a phone for free.

## Consequences

- Validation is done by calling Pydantic models on each WebSocket message by hand; FastAPI only validates HTTP routes automatically, and the hub has almost none.
- Python clients (clap detector, voice) will use the `websockets` library for the client side.
- More packages (FastAPI, Starlette, Pydantic, uvicorn) than aiohttp alone; acceptable.
- uvicorn creates the event loop. It uses the standard asyncio loop on Windows, which is what `bleak` needs.
