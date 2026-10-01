"""
Tests for protocol.py.

Every expected byte string here was copied from a REAL source: the iPhone
Bluetooth capture (frame numbers noted) or the lamp's replies during our
own experiments. If these pass, our code speaks exactly what the real app
and lamp speak.

Run:  python -m pytest
"""

from datetime import datetime

import pytest

from ilamp import protocol as p
from ilamp.protocol import LampState, Mode, SunUpdate


def hx(s: str) -> bytes:
    """Hex string (spaces allowed) -> bytes."""
    return bytes.fromhex(s.replace(" ", ""))


# ---------------------------------------------------------------------------
# Building: our packets must equal what the app sent
# ---------------------------------------------------------------------------


def test_handshake_matches_capture():
    # frames 5835 and 5836
    assert p.handshake() == [
        hx("01fe0000 0000 1800 0000000000000000 0d06ff02040e0000"),
        hx("01fe0000 0000 1800 0000000000000000 0d07ff0304010e00"),
    ]


def test_heartbeat_matches_capture():
    # frame 5862 - an empty envelope, length 0x10
    assert p.heartbeat() == hx("01fe0000 5100 1000 0000000000000000")


def test_power_matches_capture():
    assert p.power(False) == hx("01fe0000 5181 1800 0000000000000000 0d07020301020e00")  # 6086
    assert p.power(True) == hx("01fe0000 5181 1800 0000000000000000 0d07020301010e00")  # 6213


def test_color_matches_capture():
    # frame 6283: brightness ff, rgb (8a, 26, 00)
    assert p.color(0x8A, 0x26, 0x00) == hx(
        "01fe0000 5181 1c00 0000000000000000 0d0a02030cff8a26000e0000"
    )
    # frame 6607: brightness 02, rgb (00, a6, 8a)
    assert p.color(0x00, 0xA6, 0x8A, brightness=0x02) == hx(
        "01fe0000 5181 1c00 0000000000000000 0d0a02030c0200a68a0e0000"
    )


def test_color_rejects_out_of_range():
    import pytest

    with pytest.raises(ValueError):
        p.color(256, 0, 0)
    with pytest.raises(ValueError):
        p.color(0, 0, 0, brightness=-1)


def test_mode_normal_matches_capture():
    # frame 6284: sent by the app right after every color change
    assert p.mode(Mode.NORMAL) == hx("01fe0000 5181 1800 0000000000000000 0d07020304000e00")


def test_read_state_matches_capture():
    # frame 5882: the app asking for the current state on startup
    assert p.read_state() == hx("01fe0000 5181 1c00 0000000000000000 0d0c0202020000000000000e")


def test_time_sync_matches_capture():
    # frame 5844: the app setting the clock to 2026-10-01 20:50:25
    assert p.time_sync(datetime(2026, 10, 1, 20, 50, 25)) == hx(  # noqa: DTZ001 - lamp clock has no tz
        "01fe0000 5300 1800 0000000000000000 ea070a0114321900"
    )


# ---------------------------------------------------------------------------
# Parsing: real lamp replies must decode to what we saw on the lamp
# ---------------------------------------------------------------------------


def test_parse_report_after_dim_blue():
    # demo run: reply to "blue, brightness 20"
    reply = hx("01fe0000 4181 1c00 0000000000000000 0d0c0204010114 0000ff 00 0e")
    assert p.parse_state(reply) == LampState(
        on=True, brightness=20, rgb=(0, 0, 255), mode=Mode.NORMAL
    )


def test_parse_report_after_power_off():
    # demo run: reply to "power off"
    reply = hx("01fe0000 4181 1c00 0000000000000000 0d0c02040102ffff0000000e")
    state = p.parse_state(reply)
    assert state is not None and state.on is False


def test_parse_report_with_mode():
    # mode scan: reply to mode 0x04
    reply = hx("01fe0000 4181 1c00 0000000000000000 0d0c02040101ffffffff040e")
    assert p.parse_state(reply).mode == Mode.RAINBOW


def test_parse_answer_to_read_state():
    # frame 5900: the lamp answering the app's read_state() on startup
    reply = hx("01fe0000 4181 1c00 0000000000000000 0d0c02020201ff8ae700000e")
    assert p.parse_state(reply) == LampState(
        on=True, brightness=255, rgb=(0x8A, 0xE7, 0x00), mode=Mode.NORMAL
    )


def test_unknown_mode_is_kept_as_int():
    reply = hx("01fe0000 4181 1c00 0000000000000000 0d0c02040101ffffffff090e")
    assert p.parse_state(reply).mode == 9


def test_non_state_packets_parse_to_none():
    # heartbeat answer (frame 5843) - a STATUS packet, not a state report
    status = hx(
        "01fe0000 4100 2800 0000000000000000001f001f0700000800000000000000000000000001030000"
    )
    assert p.parse_state(status) is None
    # a reply that isn't about the light state (frame 5874)
    other = hx("01fe0000 4181 1700 0000000000000000 0d0702020d010e")
    assert p.parse_state(other) is None
    # garbage
    assert p.parse_state(b"hello") is None


def test_packet_type():
    assert p.packet_type(p.heartbeat()) == p.HEARTBEAT
    assert p.packet_type(p.power(True)) == p.COMMAND
    assert p.packet_type(b"\x00" * 20) is None


# ---------------------------------------------------------------------------
# The sun ring (white LEDs): group 0x01. Frames from the 2026-10-02 capture.
# ---------------------------------------------------------------------------


def test_sun_power_matches_capture():
    # frame 13481: the app turning the sun ring on
    assert p.sun_power(True) == hx("01fe0000 5181 1800 0000000000000000 0d07010301010e00")
    # never captured (the app only sent "on"); by analogy with Power.OFF, confirmed by sun_check
    assert p.sun_power(False) == hx("01fe0000 5181 1800 0000000000000000 0d07010301020e00")


def test_sun_temperature_matches_capture():
    # frame 13594: the warm/cool slider at 0x57
    assert p.sun_temperature(0x57) == hx("01fe0000 5181 1800 0000000000000000 0d07010303570e00")
    with pytest.raises(ValueError):
        p.sun_temperature(256)


def test_sun_level_matches_capture():
    # frame 15122: the app re-applying "level" 0x10 (meaning unconfirmed)
    assert p.sun_level(0x10) == hx("01fe0000 5181 1800 0000000000000000 0d07010302100e00")


def test_read_sun_state_matches_capture():
    # frame 13408: the app asking for the ring's state on startup
    assert p.read_sun_state() == hx("01fe0000 5181 1800 0000000000000000 0d0801020200000e")


def test_parse_sun_report_after_power_on():
    # frame 13483: the lamp's report after sun on: [power, level]
    reply = hx("01fe0000 4181 1800 0000000000000000 0d0801040101100e")
    assert p.parse_sun(reply) == SunUpdate(on=True, level=0x10, temperature=None)


def test_parse_sun_temperature_report():
    # frame 13598: the lamp echoing the slider
    reply = hx("01fe0000 4181 1700 0000000000000000 0d07010403570e")
    assert p.parse_sun(reply) == SunUpdate(on=None, level=None, temperature=0x57)


def test_parse_answer_to_read_sun_state():
    # frame 13427: the answer on startup, ring off
    reply = hx("01fe0000 4181 1800 0000000000000000 0d0801020202100e")
    assert p.parse_sun(reply) == SunUpdate(on=False, level=0x10, temperature=None)


def test_sun_and_light_parsers_ignore_each_other():
    sun_report = hx("01fe0000 4181 1800 0000000000000000 0d0801040101100e")
    light_report = hx("01fe0000 4181 1c00 0000000000000000 0d0c02040101ffff7359000e")  # frame 15761
    assert p.parse_state(sun_report) is None
    assert p.parse_sun(light_report) is None
    assert p.parse_sun(b"hello") is None


def test_read_sun_temperature_matches_capture():
    # frames 14980 / 14989: the app asking for the ring's temperature, and the answer
    assert p.read_sun_temperature() == hx("01fe0000 5181 1800 0000000000000000 0d07010203000e00")
    answer = hx("01fe0000 4181 1700 0000000000000000 0d070102039c0e")
    assert p.parse_sun(answer) == SunUpdate(on=None, level=None, temperature=0x9C)


def test_sun_level_is_0_to_16():
    # Confirmed on the lamp 2026-10-02: 0-16 are accepted, 17+ silently ignored.
    assert p.sun_level(0) and p.sun_level(16)
    with pytest.raises(ValueError):
        p.sun_level(17)
