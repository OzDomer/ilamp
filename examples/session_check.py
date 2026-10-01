"""
session_check.py - check the Session layer against the REAL lamp.

The unit tests prove Session works with the fake lamp. This proves the
fake lamp is honest: same steps, real hardware.

Expected: connects, lamp turns off then on, stays connected for 20s
(well past the ~14s watchdog), prints state reports, closes cleanly.

Run from the project folder:  python -m examples.session_check
"""

import asyncio

from ilamp import protocol as p
from ilamp.session import Session


def on_packet(data: bytes) -> None:
    state = p.parse_state(data)
    if state is not None:
        print(f"  <- state: {state}")


def on_disconnect() -> None:
    print("  !! the lamp dropped the connection")


async def main() -> None:
    print("Connecting...")
    async with Session(on_packet=on_packet, on_disconnect=on_disconnect) as session:
        print("Session open, heartbeat running.")

        await session.send(p.read_state())
        await asyncio.sleep(1)

        print("Power off...")
        await session.send(p.power(False))
        await asyncio.sleep(2)

        print("Power on...")
        await session.send(p.power(True))

        for seconds_left in range(20, 0, -5):
            print(f"Staying connected, {seconds_left}s left (connected: {session.is_connected})")
            await asyncio.sleep(5)

    print("Closed cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
