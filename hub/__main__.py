"""
Run the hub:  python -m hub [--host HOST] [--port PORT] [--lamp-name NAME]

Defaults can also come from the environment: ILAMP_HOST, ILAMP_PORT,
ILAMP_NAME. The server listens on all interfaces by default so a phone on
the same WiFi can open it; there is no login, so anyone on that network
can control the lamp.
"""

import argparse
import logging
import os

import uvicorn

from ilamp import Lamp

from .server import create_app
from .service import LampService


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m hub", description="The ilamp hub server.")
    parser.add_argument("--host", default=os.environ.get("ILAMP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ILAMP_PORT", "8000")))
    parser.add_argument("--lamp-name", default=os.environ.get("ILAMP_NAME", "i_Lamp"))
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    service = LampService(lamp_factory=lambda **kwargs: Lamp(name=args.lamp_name, **kwargs))
    uvicorn.run(create_app(service), host=args.host, port=args.port, log_level="info")


main()
