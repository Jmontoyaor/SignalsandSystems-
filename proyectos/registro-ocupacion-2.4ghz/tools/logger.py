#!/usr/bin/env python3
"""Record nrf24_logger sweeps to a timestamped CSV file.

Usage: python logger.py [--port /dev/ttyUSB0] [--out data/]

Each output row: timestamp (ISO 8601, local time), unix_time, sweep, sweep_ms,
passes, then one column per channel (f2400 ... f2525) with the number of RPD
detections out of `passes`. The serial link is reopened automatically if the
board is unplugged or resets, so the logger can run unattended for hours.
"""
import argparse
import csv
import os
import time
from datetime import datetime
from pathlib import Path

import serial

NUM_CHANNELS = 126
BASE_MHZ = 2400
DEFAULT_PASSES = 30
STATUS_EVERY_S = 300


def open_port(port: str) -> serial.Serial:
    while True:
        try:
            return serial.Serial(port, 115200, timeout=5)
        except serial.SerialException as exc:
            print(f"[{datetime.now():%H:%M:%S}] waiting for {port}: {exc}", flush=True)
            time.sleep(5)


def parse_passes(comment: str, current: int) -> int:
    for token in comment.split():
        if token.startswith("passes="):
            return int(token.split("=", 1)[1])
    return current


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "data"))
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"occupancy_{datetime.now():%Y%m%d_%H%M%S}.csv"
    header = ["timestamp", "unix_time", "sweep", "sweep_ms", "passes"] + [
        f"f{BASE_MHZ + ch}" for ch in range(NUM_CHANNELS)
    ]

    passes = DEFAULT_PASSES
    rows = 0
    last_status = time.time()
    print(f"Logging to {path} (Ctrl+C to stop)", flush=True)

    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        link = open_port(args.port)
        while True:
            try:
                raw = link.readline()
            except (serial.SerialException, OSError) as exc:
                print(f"[{datetime.now():%H:%M:%S}] serial error: {exc}; reconnecting", flush=True)
                link.close()
                time.sleep(2)
                link = open_port(args.port)
                continue

            line = raw.decode(errors="replace").strip()
            if line.startswith("#"):
                passes = parse_passes(line, passes)
                print(f"[{datetime.now():%H:%M:%S}] {line}", flush=True)
                continue
            fields = line.split(",")
            if len(fields) != 3 + NUM_CHANNELS or fields[0] != "S":
                continue  # partial line after a reset or noise

            now = datetime.now()
            writer.writerow([now.isoformat(timespec="seconds"), f"{now.timestamp():.1f}",
                             fields[1], fields[2], passes, *fields[3:]])
            handle.flush()
            rows += 1
            if rows % 100 == 0:
                os.fsync(handle.fileno())

            if time.time() - last_status >= STATUS_EVERY_S:
                busy = sum(int(v) for v in fields[3:]) / (NUM_CHANNELS * passes)
                print(f"[{now:%H:%M:%S}] {rows} sweeps logged, current occupancy {busy:.1%}", flush=True)
                last_status = time.time()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.")
