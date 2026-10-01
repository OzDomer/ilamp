"""
messages.py - the JSON spoken over the hub's WebSocket.

Client -> hub, one request per message. `id` is optional; the hub echoes
it in the ack or error so the client can match answers to requests:

    {"id": 1, "type": "power", "on": true}
    {"id": 2, "type": "color", "r": 255, "g": 0, "b": 0, "brightness": 180}
    {"id": 3, "type": "brightness", "value": 80}
    {"id": 4, "type": "mode", "mode": "rainbow"}
    {"id": 5, "type": "refresh"}
    {"id": 6, "type": "sun", "on": true}                 the white ring
    {"id": 7, "type": "sun_temperature", "value": 0}     0 warm .. 255 cold

Hub -> client:

    {"type": "state", "connected": true,
     "lamp": {"on": true, "brightness": 180, "rgb": [255, 120, 40], "mode": "normal"},
     "sun": {"on": false, "temperature": 128}}
    {"type": "state", "connected": false, "lamp": null}
    {"type": "ack", "id": 2}
    {"type": "error", "id": 4, "message": "..."}

Every request is validated here, by Pydantic, before anything reaches the
lamp: ranges are 0-255 and a mode must be one of the known names. The hub
only has this one channel, so validation is called by hand (FastAPI only
validates HTTP routes on its own).
"""

import json
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, field_validator

from ilamp import Mode

from .service import HubStatus

MODE_NAMES = [m.name.lower() for m in Mode]

Byte = Annotated[int, Field(ge=0, le=255)]


# -- client -> hub --------------------------------------------------------------


class _Request(BaseModel):
    model_config = {"extra": "forbid"}  # a typo in a field name is an error, not a no-op

    id: int | None = None


class PowerRequest(_Request):
    type: Literal["power"]
    on: bool


class ColorRequest(_Request):
    type: Literal["color"]
    r: Byte
    g: Byte
    b: Byte
    brightness: Byte | None = None  # None: keep the current brightness


class BrightnessRequest(_Request):
    type: Literal["brightness"]
    value: Byte


class ModeRequest(_Request):
    type: Literal["mode"]
    mode: Mode  # given by name ("rainbow"), stored as the enum

    @field_validator("mode", mode="before")
    @classmethod
    def _by_name(cls, value: object) -> Mode:
        if not isinstance(value, str) or value.upper() not in Mode.__members__:
            raise ValueError(f"unknown mode {value!r}; known modes: {', '.join(MODE_NAMES)}")
        return Mode[value.upper()]


class RefreshRequest(_Request):
    type: Literal["refresh"]


class SunRequest(_Request):
    type: Literal["sun"]
    on: bool


class SunTemperatureRequest(_Request):
    type: Literal["sun_temperature"]
    value: Byte  # 0 warm .. 255 cold


Request = (
    PowerRequest
    | ColorRequest
    | BrightnessRequest
    | ModeRequest
    | RefreshRequest
    | SunRequest
    | SunTemperatureRequest
)

# `type` picks the model, so a wrong `type` says so instead of failing all five.
_request_adapter = TypeAdapter(Annotated[Request, Field(discriminator="type")])


def parse_request(text: str) -> Request:
    """
    Parse and validate one client message.

    Raises ValueError with a short, readable reason ("r: Input should be
    less than or equal to 255") suitable for sending back to the client.
    """
    try:
        return _request_adapter.validate_json(text)
    except ValidationError as e:
        reasons = []
        for err in e.errors():
            field = ".".join(str(part) for part in err["loc"] if part not in ("tagged-union",))
            reasons.append(f"{field}: {err['msg']}" if field else err["msg"])
        raise ValueError("; ".join(reasons)) from None


def peek_id(text: str) -> int | None:
    """The `id` of a message that failed validation, if it has a usable one."""
    try:
        value = json.loads(text).get("id")
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, int) else None


# -- hub -> client --------------------------------------------------------------


def status_message(status: HubStatus) -> dict:
    lamp, sun = status.lamp, status.sun
    return {
        "type": "state",
        "connected": status.connected,
        "lamp": None
        if lamp is None
        else {
            "on": lamp.on,
            "brightness": lamp.brightness,
            "rgb": list(lamp.rgb),
            "mode": _mode_name(lamp.mode),
        },
        "sun": None if sun is None else {"on": sun.on, "temperature": sun.temperature},
    }


def ack_message(request_id: int | None) -> dict:
    return {"type": "ack", "id": request_id}


def error_message(message: str, request_id: int | None = None) -> dict:
    return {"type": "error", "id": request_id, "message": message}


def _mode_name(mode: Mode | int) -> str:
    # The lamp may report a mode value we haven't named; pass the number through.
    return mode.name.lower() if isinstance(mode, Mode) else str(mode)
