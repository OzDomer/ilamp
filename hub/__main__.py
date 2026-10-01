"""
Run the hub:  python -m hub [--host HOST] [--port PORT] [--lamp-name NAME]

Defaults can also come from the environment: ILAMP_HOST, ILAMP_PORT,
ILAMP_NAME, ILAMP_COLOR_SCALE. The server listens on all interfaces by
default so a phone on the same WiFi can open it; there is no login, so
anyone on that network can control the lamp.

--color-scale R,G,B multiplies colors before they reach the lamp. Its
green and blue overpower red, so pure white looks cyan; something like
1,0.7,0.6 balances it. Tune by eye.
"""

import argparse
import logging
import os

import uvicorn

from ilamp import Lamp

from .server import create_app
from .service import LampService


def color_scale(text: str) -> tuple[float, float, float]:
    """Parse "1,0.7,0.6" into three floats; argparse shows the error if it can't."""
    parts = [float(v) for v in text.split(",")]
    if len(parts) != 3 or any(v < 0 for v in parts):
        raise argparse.ArgumentTypeError("expected three non-negative numbers, like 1,0.7,0.6")
    return parts[0], parts[1], parts[2]


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m hub", description="The ilamp hub server.")
    parser.add_argument("--host", default=os.environ.get("ILAMP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ILAMP_PORT", "8000")))
    parser.add_argument("--lamp-name", default=os.environ.get("ILAMP_NAME", "i_Lamp"))
    parser.add_argument(
        "--color-scale",
        type=color_scale,
        default=os.environ.get("ILAMP_COLOR_SCALE", "1,1,1"),
        help="R,G,B multipliers, e.g. 1,0.7,0.6 (default: 1,1,1)",
    )
    args = parser.parse_args()
    scale = color_scale(args.color_scale) if isinstance(args.color_scale, str) else args.color_scale

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    service = LampService(
        lamp_factory=lambda **kwargs: Lamp(name=args.lamp_name, color_scale=scale, **kwargs)
    )
    uvicorn.run(create_app(service), host=args.host, port=args.port, log_level="info")


main()
