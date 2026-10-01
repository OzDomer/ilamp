"""
Tests for hub/messages.py: what clients may send, and what the hub sends.

Validation happens here, before anything reaches the lamp. A bad request
must be rejected with a message a human can act on.
"""

import pytest

from hub.messages import (
    ColorRequest,
    ModeRequest,
    SunLevelRequest,
    SunRequest,
    SunTemperatureRequest,
    parse_request,
    status_message,
)
from hub.service import HubStatus
from ilamp import LampState, Mode, SunState


def test_color_request_without_brightness_keeps_it_unset():
    req = parse_request('{"id": 3, "type": "color", "r": 255, "g": 0, "b": 0}')
    assert isinstance(req, ColorRequest)
    assert (req.id, req.r, req.g, req.b, req.brightness) == (3, 255, 0, 0, None)


def test_out_of_range_values_are_rejected():
    with pytest.raises(ValueError, match="r"):
        parse_request('{"type": "color", "r": 256, "g": 0, "b": 0}')
    with pytest.raises(ValueError, match="value"):
        parse_request('{"type": "brightness", "value": -1}')


def test_mode_is_given_by_name_and_becomes_a_mode():
    req = parse_request('{"type": "mode", "mode": "rainbow"}')
    assert isinstance(req, ModeRequest)
    assert req.mode is Mode.RAINBOW


def test_unknown_mode_error_lists_the_known_names():
    with pytest.raises(ValueError, match="candlelight"):
        parse_request('{"type": "mode", "mode": "disco"}')


def test_unknown_type_and_garbage_are_rejected():
    with pytest.raises(ValueError):
        parse_request('{"type": "explode"}')
    with pytest.raises(ValueError):
        parse_request("not json at all")


def test_status_message_is_plain_json_with_mode_names():
    state = LampState(on=True, brightness=180, rgb=(255, 120, 40), mode=Mode.NORMAL)
    assert status_message(HubStatus(connected=True, lamp=state)) == {
        "type": "state",
        "connected": True,
        "lamp": {"on": True, "brightness": 180, "rgb": [255, 120, 40], "mode": "normal"},
        "sun": None,
    }


def test_status_message_while_disconnected_has_no_lamp():
    assert status_message(HubStatus(connected=False, lamp=None)) == {
        "type": "state",
        "connected": False,
        "lamp": None,
        "sun": None,
    }


def test_sun_requests_parse():
    req = parse_request('{"id": 9, "type": "sun", "on": true}')
    assert isinstance(req, SunRequest) and req.on is True
    req = parse_request('{"type": "sun_temperature", "value": 200}')
    assert isinstance(req, SunTemperatureRequest) and req.value == 200
    with pytest.raises(ValueError, match="value"):
        parse_request('{"type": "sun_temperature", "value": 300}')


def test_status_message_carries_the_ring():
    state = LampState(on=False, brightness=180, rgb=(255, 120, 40), mode=Mode.NORMAL)
    sun = SunState(on=True, level=16, temperature=40)
    msg = status_message(HubStatus(connected=True, lamp=state, sun=sun))
    assert msg["sun"] == {"on": True, "temperature": 40, "level": 16}
    assert msg["lamp"]["on"] is False
    assert status_message(HubStatus(connected=False, lamp=None, sun=None))["sun"] is None


def test_sun_level_request_is_0_to_16():
    req = parse_request('{"type": "sun_level", "value": 16}')
    assert isinstance(req, SunLevelRequest) and req.value == 16
    with pytest.raises(ValueError, match="value"):
        parse_request('{"type": "sun_level", "value": 17}')


def test_sleep_is_a_mode_name_and_alert_is_not_advertised():
    assert parse_request('{"type": "mode", "mode": "sleep"}').mode is Mode.SLEEP
    with pytest.raises(ValueError, match="sleep"):
        parse_request('{"type": "mode", "mode": "disco"}')
