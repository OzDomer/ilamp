"""
Run the clap detector:

  python -m clap --list-devices           what microphones there are
  python -m clap --monitor [--device X]   watch the levels and events, no hub
  python -m clap [--device X] [--hub ws://localhost:8000/ws]
                                          double clap -> toggle the lamp

Thresholds (--floor, --onset-ratio, --loud) default to clap.detector.Settings;
use --monitor to pick values for your room: clap, knock, talk, play music,
and see what each does to the numbers.
"""

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import replace

from .client import HubClient
from .detector import Detector, Settings
from .mic import Microphone, find_device, list_devices


def parse_args() -> argparse.Namespace:
    defaults = Settings()
    parser = argparse.ArgumentParser(
        prog="python -m clap", description="Double clap -> lamp toggle."
    )
    parser.add_argument("--list-devices", action="store_true", help="list input devices and exit")
    parser.add_argument(
        "--device", default=os.environ.get("CLAP_DEVICE"), help="index or part of a name"
    )
    parser.add_argument(
        "--monitor", action="store_true", help="show levels and events; don't touch the hub"
    )
    parser.add_argument("--hub", default=os.environ.get("ILAMP_HUB", "ws://localhost:8000/ws"))
    parser.add_argument("--floor", type=float, default=defaults.floor)
    parser.add_argument("--onset-ratio", type=float, default=defaults.onset_ratio)
    parser.add_argument("--loud", type=float, default=defaults.loud)
    return parser.parse_args()


async def monitor(device: int | None, settings: Settings) -> None:
    """A live view: one line per 100 ms with the peak, the background, and the guard."""
    detector = Detector(settings)
    peak_since_print = 0.0
    blocks = 0
    print("peak   |background|  bar (peak)                              events")
    async with Microphone(device, settings.sample_rate, settings.block_ms) as mic:
        async for block in mic.blocks():
            events = detector.feed(block)
            peak_since_print = max(peak_since_print, detector.level)
            blocks += 1
            for event in events:
                print(f"\n{'  >>> DOUBLE CLAP <<<' if event == 'double' else '  * clap'}")
            if blocks % 5 == 0:  # every 100 ms
                bar = "#" * min(40, int(peak_since_print * 80))
                guard = "  [music: suspended]" if detector.suspended else ""
                sys.stdout.write(
                    f"\r{peak_since_print:5.3f}  | {detector.background:7.4f} | {bar:<40}{guard}"
                )
                sys.stdout.flush()
                peak_since_print = 0.0


async def listen(device: int | None, settings: Settings, hub_url: str) -> None:
    """The real thing: a double clap toggles the lamp through the hub."""
    client = HubClient(hub_url)
    hub_task = asyncio.create_task(client.run())
    detector = Detector(settings)

    async def toggle() -> None:
        try:
            await client.toggle()
            log.info("Double clap: lamp toggled.")
        except (ConnectionError, RuntimeError, TimeoutError) as e:
            log.warning("Double clap, but the hub said: %s", e)

    try:
        async with Microphone(device, settings.sample_rate, settings.block_ms) as mic:
            log.info("Listening. Clap twice to toggle the lamp.")
            async for block in mic.blocks():
                for event in detector.feed(block):
                    if event == "double":
                        asyncio.create_task(toggle())  # don't hold up the audio loop
    finally:
        hub_task.cancel()
        await asyncio.gather(hub_task, return_exceptions=True)


log = logging.getLogger("clap")


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if args.list_devices:
        for index, name in list_devices():
            print(f"{index:3d}  {name}")
        return
    device = find_device(args.device)
    settings = replace(Settings(), floor=args.floor, onset_ratio=args.onset_ratio, loud=args.loud)
    if args.monitor:
        try:
            asyncio.run(monitor(device, settings))
        except KeyboardInterrupt:
            print("\nbye")
        return
    try:
        asyncio.run(listen(device, settings, args.hub))
    except KeyboardInterrupt:
        print("bye")


main()
