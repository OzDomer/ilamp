# ilamp

Control a 10-year-old Chinese Bluetooth speaker lamp ("i_Lamp") from Python. The original app has been dead for years, so I reverse-engineered its Bluetooth protocol from scratch.

```python
import asyncio
from ilamp import Lamp, Mode

async def main():
    async with Lamp() as lamp:
        await lamp.color(255, 0, 0)        # red
        await lamp.brightness(80)
        await lamp.mode(Mode.CANDLELIGHT)
        print(lamp.state)
        # LampState(on=True, brightness=80, rgb=(255, 0, 0), mode=<Mode.CANDLELIGHT: 6>)

asyncio.run(main())
```

Every command is **confirmed**: it waits for the lamp to report the new state, and raises `CommandNotConfirmedError` if the lamp rejects it.

## Features

- Power, color, brightness, and all light modes, including a hidden **Alert** mode the official app never exposed
- Live state: `lamp.state` is always what the lamp itself last reported
- Optional color correction (`Lamp(color_scale=(1.0, 0.7, 0.6))`) for the lamp's cyan-leaning white
- Works alongside the speaker: a phone can stream music to the lamp while this library controls the lights
- 30 tests, no hardware needed: protocol tests use bytes from real captures, and connection tests run against a simulated lamp

## Install

```
git clone <this repo>
cd ilamp
pip install -e .[dev]
python -m pytest
python -m examples.demo     # needs the lamp nearby
```

Close the phone app first. The lamp accepts one controller at a time.

## How I reverse-engineered it

> **Draft. Rewrite this in your own words.**

1. **Explored the lamp over BLE** with Python + [bleak](https://github.com/hbldh/bleak). It exposed two vendor-specific characteristics (`0x8877` and `0x8888`) and a leftover `"LE Sample Device"` name, a sign the firmware was built from vendor example code.
2. **Tried to read the app.** I decompiled the Android APK with jadx. It confirmed the right UUIDs and an Actions Semiconductor chip (the "iBluz" SDK), but the method bodies were hollowed out, so the protocol wasn't readable.
3. **Watched the traffic instead.** I captured the iPhone app's Bluetooth traffic (Apple's Bluetooth logging profile + sysdiagnose) while doing one slow, deliberate action at a time: off, on, red, green, blue, dim, bright. Then I decoded the packets in Wireshark by comparing what stayed fixed against what changed.
4. **Tested hypotheses one at a time.** My replayed commands got the connection killed. I tested a session-ID theory (wrong, the bytes turned out to be a packet type with a direction bit) and an MTU theory (wrong, MTU was 200). The real answer was a **heartbeat**: the lamp hangs up after ~14 s without one.
5. **Mapped the modes by targeted fuzzing**: only the suspected mode command, only its one argument, with the lamp's own state reports grading each value. That surfaced a sixth mode the app never shows.

## Protocol reference

All traffic goes through two characteristics:

| UUID | Direction | Use |
|---|---|---|
| `00008877-…` | client → lamp | write commands (Write Without Response) |
| `00008888-…` | lamp → client | notifications (replies, status) |

**Packet layout**: a 16-byte wrapper, then usually an inner message:

```
01 fe 00 00 | TT TT | LL LL | 00 x 8 | 0d nn gg oo cc [args…] 0e [zero padding]
  header      type    length  reserved  inner message
```

- **Type**: `00 00` handshake · `51 81` command · `41 81` reply · `51 00` heartbeat · `41 00` status · `53 00` clock sync. The first byte's `0x10` bit marks direction (to the lamp / from the lamp).
- **Length**: total packet length, little-endian.
- **Inner**: `0d` start, length, group (`02` light, `ff` system), operation (`02` read, `03` write, `04` report), command, arguments, `0e` end. Outgoing messages are zero-padded to a multiple of 4 bytes.

**Session**: write ASCII `01234567`, then two handshake packets, then a heartbeat (`01 fe 00 00 51 00 10 00` + 8 zero bytes) every ~0.5 s. A command sent before the handshake gets the connection dropped.

| Command | Inner message |
|---|---|
| Power on / off | `02 03 01 01` / `02 03 01 02` |
| Color + brightness | `02 03 0c [brightness] [R] [G] [B]` |
| Mode | `02 03 04 [mode]` |
| Read state | `02 02 02 00 00 00 00 00 00` |

**Modes**: `00` Normal · `01` Pulse · `03` Rhythm (follows audio streamed to the speaker) · `04` Rainbow · `06` Candlelight · `07` Alert (hidden). Other values are ignored.

**State reports** (`02 04 01 …` after a change, `02 02 02 …` in answer to a read) carry `[power] [brightness] [R] [G] [B] [mode]`. The lamp **only reports changes**, so a command that changes nothing gets no reply.

## Project layout

```
ilamp/protocol.py   bytes only: build packets, parse replies (no Bluetooth)
ilamp/session.py    Bluetooth only: connect, handshake, background heartbeat
ilamp/lamp.py       the public API: confirmed commands and live state
tests/fake_lamp.py  a simulated lamp that behaves like the real one
examples/           demo.py, and session_check.py for real hardware
```

The layers are separate so each can change on its own. Porting to a microcontroller means rewriting only `session.py`, and the protocol stays testable without hardware.
