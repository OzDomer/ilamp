"""
sun_check.py - confirm the sun ring (white LEDs) hypotheses on the REAL lamp.

The 2026-10-02 capture showed the ring is message group 0x01. This script
sends each guessed command, one variable at a time, and prints what the
lamp reports back. Watch the lamp and note what you see at each step:

  1. the ring's state before we touch it
  2. sun ON              (captured)        -> does the RGB light go off by itself?
  3. temperature 0 / 255 (captured 02..fa) -> which end is warm, which is cool?
  4. level 0x10 / 0x80 / 0xff / 0x01       -> does the ring's brightness change?
  5. RGB power ON while the ring is on     -> does the ring go off?
  6. sun OFF             (never captured; built by analogy with Power.OFF)

No prompts: a blocking input() would starve the heartbeat and the lamp
would hang up. Steps are spaced ~5 s apart. Run: python -m examples.sun_check
"""

import asyncio

from ilamp import protocol as p
from ilamp.session import Session

PAUSE = 5


def on_packet(data: bytes) -> None:
    sun = p.parse_sun(data)
    if sun is not None:
        print(f"      <- sun: {sun}")
    state = p.parse_state(data)
    if state is not None:
        print(f"      <- rgb: {state}")


async def step(session: Session, label: str, *packets: bytes, pause: float = PAUSE) -> None:
    print(f"\n[{label}]")
    for packet in packets:
        await session.send(packet)
        await asyncio.sleep(0.4)
    await asyncio.sleep(pause)


async def main() -> None:
    print("Connecting...")
    async with Session(on_packet=on_packet, on_disconnect=lambda: print("  !! dropped")) as s:
        await step(s, "1. read both states", p.read_sun_state(), p.read_state(), pause=2)

        await step(s, "2. sun ON (watch: does the RGB light go off?)", p.sun_power(True))
        await step(s, "   ...then read the RGB state", p.read_state(), pause=2)

        await step(s, "3a. temperature 0   (warm or cool?)", p.sun_temperature(0))
        await step(s, "3b. temperature 255 (warm or cool?)", p.sun_temperature(255))
        await step(s, "3c. temperature 128", p.sun_temperature(128))

        for level in (0x10, 0x80, 0xFF, 0x01, 0x10):
            await step(s, f"4. level 0x{level:02x} (does brightness change?)", p.sun_level(level))

        await step(s, "5. RGB power ON (does the ring go off?)", p.power(True))
        await step(s, "   ...then read the sun state", p.read_sun_state(), pause=2)

        await step(s, "6. sun OFF (never captured; does it work?)", p.sun_power(False))
        await step(s, "   ...then read both states", p.read_sun_state(), p.read_state(), pause=2)

        await step(s, "7. back to a warm RGB", p.power(True), p.color(255, 120, 40, 180), pause=1)
    print("\nClosed cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
