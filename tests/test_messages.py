"""
Tests for hub/messages.py: what clients may send, and what the hub sends.

Validation happens here, before anything reaches the lamp. A bad request
must be rejected with a message a human can act on.
"""

import pytest
from hub.messages import ColorRequest, ModeRequest, parse_request, status_message

from hub.service import HubStatus
from ilamp import LampState, Mode


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
    }


def test_status_message_while_disconnected_has_no_lamp():
    assert status_message(HubStatus(connected=False, lamp=None)) == {
        "type": "state",
        "connected": False,
        "lamp": None,
    }
