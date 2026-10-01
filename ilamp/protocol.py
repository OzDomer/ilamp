"""
protocol.py - the i_Lamp wire format.

This module only turns intentions into bytes and bytes into meaning.
It knows NOTHING about Bluetooth: no connecting, no sending, no waiting.
That makes it trivial to test (bytes in, bytes out) and reusable anywhere
the same lamp protocol is spoken, e.g. later from an ESP32.

Everything here was decoded from a Bluetooth capture of the original
iPhone app, then confirmed by experiments against the real lamp.

Packet anatomy
--------------
Every packet has a 16-byte outer wrapper:

    01 fe 00 00   fixed header
    TT TT         packet type (see PacketType)
    LL LL         total packet length, little-endian
    00 x 8        reserved

followed (for most types) by an inner message:

    0d            start marker
    nn            inner length, start marker to end marker inclusive
    gg            group  (0x02 = the light, 0xff = session/system)
    oo            operation (read / write / report)
    cc            command (power, color, mode, ...)
    ...           arguments
    0e            end marker
    00 ...        zero padding to a multiple of 4 bytes (only when we send)
"""

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HEADER = bytes([0x01, 0xFE, 0x00, 0x00])
WRAPPER_SIZE = 16  # header (4) + type (2) + length (2) + reserved (8)

START = 0x0D
END = 0x0E

# The very first thing the app writes after connecting: the ASCII text
# "01234567". The lamp gives no direct reply, but it's part of opening
# a session, so we send it too.
HELLO = b"01234567"


class PacketType(bytes):
    """
    The two type bytes after the header.

    The first byte encodes direction: 0x5? = to the lamp, 0x4? = from the
    lamp (they differ by one bit, 0x10). The second byte is 0x81 when the
    packet carries an inner message, 0x00 when it doesn't.
    """


HANDSHAKE = PacketType(b"\x00\x00")  # session setup, before anything else
COMMAND = PacketType(b"\x51\x81")  # us -> lamp, with inner message
REPLY = PacketType(b"\x41\x81")  # lamp -> us, with inner message
HEARTBEAT = PacketType(b"\x51\x00")  # us -> lamp, empty: "still here"
STATUS = PacketType(b"\x41\x00")  # lamp -> us, answer to each heartbeat
TIME_SYNC = PacketType(b"\x53\x00")  # us -> lamp, sets the lamp's clock


class Group(IntEnum):
    SUN = 0x01  # the white ring (separate LEDs), found in the 2026-10-02 capture
    LIGHT = 0x02  # the RGB light
    SYSTEM = 0xFF


class Op(IntEnum):
    READ = 0x02  # "what is the current value?"
    WRITE = 0x03  # "set this value"
    REPORT = 0x04  # lamp -> us: "my state just changed"


class Cmd(IntEnum):
    POWER = 0x01
    STATE = 0x02  # the full light state (used with READ)
    MODE = 0x04
    COLOR = 0x0C


class SunCmd(IntEnum):
    """Commands of the sun ring (Group.SUN)."""

    POWER = 0x01  # WRITE [Power]; REPORT [power, level]
    LEVEL = 0x02  # WRITE [0-16] brightness; a READ of it answers [power, level]
    TEMPERATURE = 0x03  # WRITE/REPORT [0-255]: 0 warm (yellowish) .. 255 cold white


class Power(IntEnum):
    ON = 0x01
    OFF = 0x02


class Mode(IntEnum):
    """Light modes, mapped by fuzzing the mode command and watching the lamp."""

    NORMAL = 0x00  # steady color
    PULSE = 0x01  # breathes through colors
    RHYTHM = 0x03  # follows audio streamed to the lamp's speaker
    RAINBOW = 0x04  # cycles through colors
    CANDLELIGHT = 0x06  # flickers
    SLEEP = (
        0x07  # slow fade, blinks 3x. Not in the app's mode list; it sends it for the sleep timer
    )
    ALERT = 0x07  # the old name for SLEEP, from before the capture explained it (an alias)


# ---------------------------------------------------------------------------
# Building packets (what we send)
# ---------------------------------------------------------------------------


def wrap(ptype: PacketType, body: bytes = b"") -> bytes:
    """Put the 16-byte outer wrapper around a body (which may be empty)."""
    length = (WRAPPER_SIZE + len(body)).to_bytes(2, "little")
    return HEADER + ptype + length + bytes(8) + body


def inner(group: int, op: int, cmd: int, args: bytes | list[int] = b"") -> bytes:
    """
    Build an inner message: 0d len group op cmd args... 0e, zero-padded to a
    multiple of 4 bytes (the app always pads; the lamp's replies don't).
    """
    msg = bytearray([START, 0, group, op, cmd, *args, END])
    msg[1] = len(msg)
    msg += bytes((-len(msg)) % 4)
    return bytes(msg)


def handshake() -> list[bytes]:
    """The two session-setup packets the app sends right after the hello."""
    return [
        wrap(HANDSHAKE, inner(Group.SYSTEM, Op.READ, 0x04)),
        wrap(HANDSHAKE, inner(Group.SYSTEM, Op.WRITE, 0x04, [0x01])),
    ]


def heartbeat() -> bytes:
    """Keep-alive. Without one every ~0.5s, the lamp hangs up after ~14s."""
    return wrap(HEARTBEAT)


def power(on: bool) -> bytes:
    value = Power.ON if on else Power.OFF
    return wrap(COMMAND, inner(Group.LIGHT, Op.WRITE, Cmd.POWER, [value]))


def color(r: int, g: int, b: int, brightness: int = 255) -> bytes:
    """Set color and brightness together; the lamp treats them as one value."""
    for name, v in (("r", r), ("g", g), ("b", b), ("brightness", brightness)):
        if not 0 <= v <= 255:
            raise ValueError(f"{name} must be 0-255, got {v}")
    return wrap(COMMAND, inner(Group.LIGHT, Op.WRITE, Cmd.COLOR, [brightness, r, g, b]))


def mode(m: Mode | int) -> bytes:
    return wrap(COMMAND, inner(Group.LIGHT, Op.WRITE, Cmd.MODE, [int(m)]))


def read_state() -> bytes:
    """Ask for the full light state. The app sends this on startup."""
    return wrap(COMMAND, inner(Group.LIGHT, Op.READ, Cmd.STATE, bytes(6)))


# -- the sun ring -------------------------------------------------------------


def sun_power(on: bool) -> bytes:
    """
    Turn the white ring on or off. The lamp turns the RGB light off by
    itself when the ring comes on: they never run together.
    """
    value = Power.ON if on else Power.OFF
    return wrap(COMMAND, inner(Group.SUN, Op.WRITE, SunCmd.POWER, [value]))


def sun_temperature(value: int) -> bytes:
    """The ring's warm <-> cool mix, 0-255."""
    _check_byte("temperature", value)
    return wrap(COMMAND, inner(Group.SUN, Op.WRITE, SunCmd.TEMPERATURE, [value]))


SUN_LEVEL_MAX = 16  # the lamp ignores anything above (confirmed by a sweep on the real lamp)


def sun_level(value: int) -> bytes:
    """The ring's brightness, 0-16. 17 steps, and very visible ones."""
    if not 0 <= value <= SUN_LEVEL_MAX:
        raise ValueError(f"level must be 0-{SUN_LEVEL_MAX}, got {value}")
    return wrap(COMMAND, inner(Group.SUN, Op.WRITE, SunCmd.LEVEL, [value]))


def read_sun_state() -> bytes:
    """Ask for the ring's [power, level]. The app sends this on startup."""
    return wrap(COMMAND, inner(Group.SUN, Op.READ, SunCmd.LEVEL, [0, 0]))


def read_sun_temperature() -> bytes:
    """Ask for the ring's temperature (frame 14980 in the capture)."""
    return wrap(COMMAND, inner(Group.SUN, Op.READ, SunCmd.TEMPERATURE, [0]))


def _check_byte(name: str, value: int) -> None:
    if not 0 <= value <= 255:
        raise ValueError(f"{name} must be 0-255, got {value}")


def time_sync(when: datetime) -> bytes:
    """
    Set the lamp's clock (it has an alarm feature). Unlike other packets,
    the body is raw - no 0d/0e framing:

        year (2 bytes, little-endian), month, day, hour, minute, second, pad
    """
    body = when.year.to_bytes(2, "little") + bytes(
        [when.month, when.day, when.hour, when.minute, when.second, 0]
    )
    return wrap(TIME_SYNC, body)


# ---------------------------------------------------------------------------
# Parsing packets (what the lamp sends)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LampState:
    """The lamp's full light state, as reported by the lamp itself."""

    on: bool
    brightness: int
    rgb: tuple[int, int, int]
    mode: Mode | int  # an int if the lamp reports a value we haven't named

    @classmethod
    def from_payload(cls, payload: bytes) -> "LampState":
        power_byte, brightness, r, g, b, mode_byte = payload[:6]
        try:
            m: Mode | int = Mode(mode_byte)
        except ValueError:
            m = mode_byte
        return cls(power_byte == Power.ON, brightness, (r, g, b), m)


@dataclass(frozen=True)
class SunUpdate:
    """
    A partial update about the sun ring. The lamp reports the ring in two
    separate messages (power + level, and temperature), so one update only
    knows the fields its message carried; the others are None.
    """

    on: bool | None
    level: int | None
    temperature: int | None


@dataclass(frozen=True)
class SunState:
    """The ring's full state, merged from the lamp's partial updates."""

    on: bool
    level: int  # brightness, 0-16
    temperature: int | None  # 0 warm .. 255 cold; None until the lamp has said

    def apply(self, update: SunUpdate) -> "SunState":
        return SunState(
            on=self.on if update.on is None else update.on,
            level=self.level if update.level is None else update.level,
            temperature=(self.temperature if update.temperature is None else update.temperature),
        )


def packet_type(data: bytes) -> PacketType | None:
    """Return the type of a packet, or None if it isn't one of ours."""
    if len(data) < WRAPPER_SIZE or data[:4] != HEADER:
        return None
    return PacketType(data[4:6])


def parse_state(data: bytes) -> LampState | None:
    """
    Extract the light state from a lamp reply, if it contains one.

    Two kinds of reply carry the same 6-byte state payload
    (power, brightness, R, G, B, mode):

      - a REPORT, sent after any change:  0d 0c 02 04 01 <payload> 0e
      - the answer to read_state():       0d 0c 02 02 02 <payload> 0e
    """
    if packet_type(data) != REPLY:
        return None
    msg = data[WRAPPER_SIZE:]
    if len(msg) < 12 or msg[0] != START or msg[2] != Group.LIGHT:
        return None
    op, cmd = msg[3], msg[4]
    is_report = op == Op.REPORT
    is_state_answer = op == Op.READ and cmd == Cmd.STATE
    if not (is_report or is_state_answer):
        return None
    return LampState.from_payload(msg[5:11])


def parse_sun(data: bytes) -> SunUpdate | None:
    """
    Extract a sun-ring update from a lamp reply, if it carries one:

      - report after a power change:   0d 08 01 04 01 <power> <level> 0e
      - answer to read_sun_state():    0d 08 01 02 02 <power> <level> 0e
      - report after a slider change:  0d 07 01 04 03 <temperature> 0e
      - answer to read_sun_temperature(): 0d 07 01 02 03 <temperature> 0e
    """
    if packet_type(data) != REPLY:
        return None
    msg = data[WRAPPER_SIZE:]
    if len(msg) < 7 or msg[0] != START or msg[2] != Group.SUN:
        return None
    op, cmd, args = msg[3], msg[4], msg[5 : msg[1] - 1]
    power_and_level = (op == Op.REPORT and cmd == SunCmd.POWER) or (
        op == Op.READ and cmd == SunCmd.LEVEL
    )
    if power_and_level and len(args) >= 2:
        return SunUpdate(on=args[0] == Power.ON, level=args[1], temperature=None)
    if cmd == SunCmd.TEMPERATURE and op in (Op.REPORT, Op.READ) and len(args) >= 1:
        return SunUpdate(on=None, level=None, temperature=args[0])
    return None
