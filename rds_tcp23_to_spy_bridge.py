#!/usr/bin/env python3
"""Bridge SDR# RDSOutput TCP ASCII-G data into an RDS Spy .spy file.

This lets an existing RDS Monitor keep using its proven Spy File input mode,
without running RDS Spy. It connects directly to SDR# RDSOutput at TCP
127.0.0.1:23, converts records such as:

    G:\r\n694A20E220574156\r\n\r\n
into .spy lines such as:

    694A 20E2 2057 4156

Default output: settings\\recording.spy next to this script

Examples:
  python rds_tcp23_to_spy_bridge.py
  python rds_tcp23_to_spy_bridge.py --host 127.0.0.1 --port 23 --output recording.spy
"""

from __future__ import annotations

import argparse
import re
import socket
import time
from datetime import datetime
from pathlib import Path

GROUP_RE = re.compile(rb"^G:\r?\n([0-9A-Fa-f]{16})$")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write SDR# RDSOutput TCP groups to an RDS Spy .spy file.")
    parser.add_argument("--host", default="127.0.0.1", help="RDSOutput server address (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=23, help="RDSOutput TCP port (default: 23).")
    parser.add_argument("--output", default=str(Path(__file__).resolve().parent / "settings" / "recording.spy"), help="Target .spy file.")
    parser.add_argument("--append", action="store_true", help="Append to output instead of starting a new capture file.")
    return parser.parse_args()


def main() -> int:
    args = arguments()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.append else "w"

    print(f"SDR# RDSOutput: TCP {args.host}:{args.port}")
    print(f"Writing RDS Spy groups to: {output}")
    print("Press Ctrl+C to stop.")

    total = 0
    reconnects = 0
    with output.open(mode, encoding="ascii", newline="\n") as spy:
        spy.write(f"# RDS TCP bridge started {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        spy.flush()
        try:
            while True:
                try:
                    with socket.create_connection((args.host, args.port), timeout=5) as sock:
                        reconnects += 1
                        sock.settimeout(2)
                        buffer = b""
                        print(f"Connected (session {reconnects}).")
                        while True:
                            try:
                                chunk = sock.recv(4096)
                            except socket.timeout:
                                continue
                            if not chunk:
                                raise ConnectionError("RDSOutput closed the connection")
                            buffer += chunk
                            while b"\r\n\r\n" in buffer:
                                record, buffer = buffer.split(b"\r\n\r\n", 1)
                                match = GROUP_RE.match(record)
                                if not match:
                                    continue
                                raw = match.group(1).decode("ascii").upper()
                                spy_line = f"{raw[0:4]} {raw[4:8]} {raw[8:12]} {raw[12:16]}\n"
                                spy.write(spy_line)
                                spy.flush()
                                total += 1
                                if total <= 10 or total % 100 == 0:
                                    print(f"#{total} {spy_line.rstrip()}")
                except (OSError, ConnectionError) as exc:
                    print(f"Connection unavailable: {exc}; retrying in 2 seconds.")
                    time.sleep(2)
        except KeyboardInterrupt:
            print(f"\nStopped. Wrote {total} RDS groups.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
