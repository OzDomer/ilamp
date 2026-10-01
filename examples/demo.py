"""
demo.py - the whole library in one tour, on the REAL lamp.

No packets, handshakes or heartbeats in sight. That's what the layers buy you.

Run from the project folder:  python -m examples.demo
"""

import asyncio

from ilamp import CommandNotConfirmedError, Lamp, Mode


async def main() -> None:
    async with Lamp() as lamp:
        print(f"Connected. Lamp says: {lamp.state}")

        await lamp.off()
        await asyncio.sleep(1.5)
        await lamp.on()

        for name, rgb in [("red", (255, 0, 0)), ("green", (0, 255, 0)), ("blue", (0, 0, 255))]:
            print(f"{name}...")
            await lamp.color(*rgb, brightness=255)
            await asyncio.sleep(1.5)

        print("dim, then bright...")
        await lamp.brightness(20)
        await asyncio.sleep(1.5)
        await lamp.brightness(255)
        await asyncio.sleep(1.5)

        print("sun ring: on (the RGB light goes off by itself), warm, cold, off...")
        await lamp.sun_on()
        print(f"  ring: {lamp.sun}")
        print(f"  rgb:  {lamp.state}")
        await asyncio.sleep(2)
        await lamp.sun_temperature(0)
        await asyncio.sleep(2)
        await lamp.sun_temperature(255)
        await asyncio.sleep(2)
        await lamp.sun_off()
        await lamp.on()
        print(f"  ring: {lamp.sun}")
        print(f"  rgb:  {lamp.state}")

        for mode in (Mode.RAINBOW, Mode.PULSE, Mode.CANDLELIGHT, Mode.SLEEP):
            print(f"mode: {mode.name.lower()}...")
            await lamp.mode(mode)
            await asyncio.sleep(5)

        # A value the lamp rejects - the library tells us instead of
        # silently doing nothing.
        try:
            await lamp.mode(0x02)
        except CommandNotConfirmedError as e:
            print(f"As expected: {e}")

        await lamp.mode(Mode.NORMAL)
        await lamp.color(255, 120, 40, brightness=180)  # warm, cozy
        print(f"Done. Final state: {lamp.state}")


if __name__ == "__main__":
    asyncio.run(main())
