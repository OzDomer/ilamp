# ilamp

Control a 10-year-old Chinese Bluetooth speaker lamp ("i_Lamp") from Python, or from any phone on your WiFi. The original app has been dead for years, so I reverse-engineered its Bluetooth protocol from scratch.

```python
import asyncio
from ilamp import Lamp, Mode


async def main():
    async with Lamp() as lamp:
        await lamp.color(255, 0, 0)  # red
        await lamp.brightness(80)
        await lamp.mode(Mode.CANDLELIGHT)
        await lamp.sun_on()  # the white ring: real room light
        await lamp.sun_temperature(0)  # warm
        print(lamp.state, lamp.sun)


asyncio.run(main())
```

Every command is **confirmed**: it waits for the lamp to report the new state, and raises `CommandNotConfirmedError` if the lamp rejects it.

## Features

- Two lights: the RGB mood light (power, color, brightness, all modes, including a hidden mode the official app never exposed) and the white ring for actual room light (power, warm/cold temperature, 17-step brightness)
- Live state: `lamp.state` and `lamp.sun` are always what the lamp itself last reported
- A **hub**: one server owns the lamp, any number of browsers control it over a WebSocket, and every screen shows what the lamp actually did. Works from a phone on the same WiFi
- A **clap detector**: double clap near the PC's microphone to toggle the lamp. It is just another client of the hub
- Optional color correction for the RGB light's cyan-leaning white
- Works alongside the speaker: a phone can stream music to the lamp while this controls the lights
- 104 tests, no hardware needed: protocol tests use bytes from real captures, the clap detector is tested on synthetic audio, and everything else runs against a simulated lamp

## Install

```
git clone <this repo>
cd ilamp
pip install -e .[dev,hub,clap]
python -m pytest
python -m examples.demo     # needs the lamp nearby
```

Close the phone app first. The lamp accepts one controller at a time.

## The hub

```
python -m hub
```

Then open http://localhost:8000 on the PC, or `http://<the PC's LAN address>:8000` on a phone. The page shows the lamp as a disc that renders whatever the lamp reports: its color and brightness, the mode's motion, or the ring's white. Tap the disc for power; sliders and buttons for the rest. Several devices can be open at once; they all follow the lamp.

Options: `--host`, `--port`, `--lamp-name`, `--color-scale R,G,B` (also as `ILAMP_HOST`, `ILAMP_PORT`, `ILAMP_NAME`, `ILAMP_COLOR_SCALE`). The hub reconnects on its own if the lamp goes away, and never stops trying.

The server listens on all interfaces with no login: anyone on the WiFi can control the lamp.

The page's JavaScript is compiled from TypeScript (`hub/ui/app.ts`); the compiled file is committed, so running the hub needs no Node. To change the UI: `npm install`, edit, `npm run build`.

## The clap detector

```
python -m clap --monitor     watch the levels and events; tune in your room
python -m clap               double clap -> toggle the lamp (the hub must be running)
```

A clap is a sharp spike from quiet that dies fast. The detector checks four things per 20 ms block: the peak is far above the room's background level and above a floor; the blocks just before were quiet; the level is back down within ~80 ms (speech, music and a vacuum cleaner fail this); and nothing counts for 150 ms after a clap (its echo). Two claps 0.2–0.8 s apart make a double; single claps are ignored on purpose, too many things sound like one. Sustained loud sound (the lamp's own speaker) suspends detection until it is quiet again.

`--device` picks a microphone (`--list-devices`); `--floor`, `--onset-ratio` and `--loud` adjust the thresholds. The detector never touches the lamp: it sends the hub the same request the page's Off control would.

## How I reverse-engineered it

1. **Explored the lamp over BLE** with Python + [bleak](https://github.com/hbldh/bleak). It exposed two vendor-specific characteristics (`0x8877` and `0x8888`) and a leftover `"LE Sample Device"` name, a sign the firmware was built from vendor example code.
2. **Tried to read the app.** I decompiled the Android APK with jadx. It confirmed the right UUIDs and an Actions Semiconductor chip (the "iBluz" SDK), but the method bodies were hollowed out, so the protocol wasn't readable.
3. **Watched the traffic instead.** I captured the iPhone app's Bluetooth traffic (Apple's Bluetooth logging profile + sysdiagnose) while doing one slow, deliberate action at a time: off, on, red, green, blue, dim, bright. Then I decoded the packets in Wireshark by comparing what stayed fixed against what changed.
4. **Tested hypotheses one at a time.** My replayed commands got the connection killed. I tested a session-ID theory (wrong, the bytes turned out to be a packet type with a direction bit) and an MTU theory (wrong, MTU was 200). The real answer was a **heartbeat**: the lamp hangs up after ~14 s without one.
5. **Mapped the modes by targeted fuzzing**: only the suspected mode command, only its one argument, with the lamp's own state reports grading each value. That surfaced a sixth mode the app never shows.
6. **Captured again for the white ring, the alarms and the sleep timer.** The ring turned out to be a whole message group of its own. The alarms and the sleep timer turned out to live in the phone: nothing about them ever reaches the lamp. A sweep on the real lamp then settled what the capture couldn't (which slider end is warm, and that the ring's brightness has exactly 17 steps).

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
- **Inner**: `0d` start, length, group (`01` the white ring, `02` the RGB light, `ff` system), operation (`02` read, `03` write, `04` report), command, arguments, `0e` end. Outgoing messages are zero-padded to a multiple of 4 bytes.

**Session**: write ASCII `01234567`, then two handshake packets, then a heartbeat (`01 fe 00 00 51 00 10 00` + 8 zero bytes) every ~0.5 s. A command sent before the handshake gets the connection dropped.

### The RGB light (group `02`)

| Command | Inner message |
|---|---|
| Power on / off | `02 03 01 01` / `02 03 01 02` |
| Color + brightness | `02 03 0c [brightness] [R] [G] [B]` |
| Mode | `02 03 04 [mode]` |
| Read state | `02 02 02 00 00 00 00 00 00` |

**Modes**: `00` Normal · `01` Pulse · `03` Rhythm (follows audio streamed to the speaker) · `04` Rainbow · `06` Candlelight · `07` Sleep (not in the app's mode list; it is what the app sends when you set a sleep timer). Other values are ignored.

**State reports** (`02 04 01 …` after a change, `02 02 02 …` in answer to a read) carry `[power] [brightness] [R] [G] [B] [mode]`. The lamp **only reports changes**, so a command that changes nothing gets no reply.

### The white ring (group `01`)

| Command | Inner message | Reply |
|---|---|---|
| Power on / off | `01 03 01 01` / `01 03 01 02` | report `01 04 01 [power] [level]`, and on power-on also `01 04 03 [temperature]` |
| Temperature | `01 03 03 [0–255]` (`0` warm, yellowish · `255` cold white) | report `01 04 03 [temperature]` |
| Brightness | `01 03 02 [0–16]` (17 visible steps; larger values are ignored) | report `01 04 01 [power] [level]` |
| Read power + brightness | `01 02 02 00 00` | `01 02 02 [power] [level]` |
| Read temperature | `01 02 03 00` | `01 02 03 [temperature]` |

The lamp runs **one light at a time**: turning the ring on switches the RGB light off and vice versa, and it does **not** report the one it switched off. The library infers it.

### What is not in the lamp

- **Alarms** and the **sleep timer** are kept by the phone. Setting them sends no time or duration to the lamp; the app only asks the lamp for its list of alarm sounds (`alarm1.mp3`, ...) and, for sleep, switches the light to mode `07`.
- **Shake** is the phone's accelerometer sending ordinary commands.

## Project layout

```
ilamp/protocol.py   bytes only: build packets, parse replies (no Bluetooth)
ilamp/session.py    Bluetooth only: connect, handshake, background heartbeat
ilamp/lamp.py       the library's public API: confirmed commands, live state for both lights
hub/service.py      the one owner of the lamp: reconnects, broadcasts state (no web code)
hub/messages.py     the JSON spoken over the WebSocket, validated with Pydantic
hub/server.py       FastAPI: one /ws endpoint and the static UI
hub/ui/app.ts       the page's TypeScript; compiled into hub/static/
clap/detector.py    audio blocks in, clap events out (no microphone, no network)
clap/mic.py         the microphone as an async stream of blocks
clap/client.py      one more WebSocket client of the hub: double clap -> toggle
tests/fake_lamp.py  a simulated lamp that behaves like the real one, quirks included
examples/           demo.py, session_check.py and sun_check.py for real hardware
docs/adr/           why things are the way they are
```

The layers are separate so each can change on its own. Porting to a microcontroller means rewriting only `session.py`, and the protocol stays testable without hardware.
