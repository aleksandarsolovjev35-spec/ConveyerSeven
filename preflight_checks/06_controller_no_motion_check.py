from __future__ import annotations

import os

from common import print_header, require
from hardware.port_discovery import find_controller


def main() -> int:
    print_header("06 — CONTROLLER PROTOCOL CHECK (I2 ONLY, NO MOTION)")
    baudrate = int(os.environ.get("SERIAL_BAUD", "115200"))
    preferred = os.environ.get("SERIAL_PORT", "COM4")
    port, message = find_controller(
        baudrate=baudrate,
        preferred_port=preferred,
    )
    require(port is not None, message)
    # find_controller accepts a port only after a valid I2 MOV/WAIT/lastErr reply.
    require("Found on" in message, f"Unexpected controller result: {message}")
    print(f"controller_port={port}")
    print(f"baudrate={baudrate}")
    print(f"result={message}")
    print("Commands sent: I2 only")
    print("CONTROLLER NO-MOTION CHECK PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
