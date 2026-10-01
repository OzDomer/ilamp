"""
Tests for protocol.py.

Every expected byte string here was copied from a REAL source: the iPhone
Bluetooth capture (frame numbers noted) or the lamp's replies during our
own experiments. If these pass, our code speaks exactly what the real app
and lamp speak.

Run:  python -m pytest
"""

from datetime import datetime

from ilamp import protocol as p
from ilamp.protocol import LampState, Mode


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
