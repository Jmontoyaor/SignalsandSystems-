#!/usr/bin/env python3
"""Colored, audible waterfall for the nrf24_spectrum sketch.

Usage: ~/.platformio/penv/bin/python spectrum_view.py [/dev/ttyUSB0] [--mute]
Close the Arduino IDE Serial Monitor first (the port can only have one reader).

Sound (played through `pacat`):
  - pitch follows the busiest frequency (2400 MHz = low, 2525 MHz = high)
  - Geiger-like clicks and static grow with total 2.4 GHz activity
"""
import array
import math
import random
import shutil
import subprocess
import sys
import threading

import serial

LEVELS = " .:-=+*#%@"
# 256-color ANSI background ramp: dark blue -> cyan -> green -> yellow -> red.
COLORS = [17, 18, 25, 31, 37, 41, 76, 184, 208, 196]
RESET = "\033[0m"

SAMPLE_RATE = 22050
CHUNK = SAMPLE_RATE // 20  # 50 ms of audio per write
FULL_SCALE_ACTIVITY = 120  # sum of row levels considered "very busy"


class RadioSound:
    """Generates audio continuously from the latest sweep state."""

    def __init__(self) -> None:
        self.activity = 0.0  # 0..1
        self.pitch = 0.0     # Hz, 0 = no peak
        self._phase = 0.0
        self._lock = threading.Lock()
        self._player = subprocess.Popen(
            ["pacat", "--raw", "--format=s16le", f"--rate={SAMPLE_RATE}",
             "--channels=1", "--latency-msec=150"],
            stdin=subprocess.PIPE,
        )
        threading.Thread(target=self._run, daemon=True).start()

    def update(self, levels: list[int], peak_mhz: int | None) -> None:
        with self._lock:
            self.activity = min(1.0, sum(levels) / FULL_SCALE_ACTIVITY)
            self.pitch = 220 + (peak_mhz - 2400) * 12 if peak_mhz else 0.0

    def _run(self) -> None:
        while True:
            with self._lock:
                activity, pitch = self.activity, self.pitch
            samples = array.array("h")
            tone_amp = 0.22 * math.sqrt(activity) if pitch else 0.0
            noise_amp = 0.02 + 0.10 * activity
            click_prob = 0.0004 + 0.006 * activity
            step = 2 * math.pi * pitch / SAMPLE_RATE
            click_left = 0
            for _ in range(CHUNK):
                value = tone_amp * math.sin(self._phase)
                self._phase = (self._phase + step) % (2 * math.pi)
                value += noise_amp * (random.random() * 2 - 1)
                if click_left == 0 and random.random() < click_prob:
                    click_left = 40
                if click_left:
                    value += 0.5 * (1 if click_left > 20 else -1) * click_left / 40
                    click_left -= 1
                samples.append(int(max(-1.0, min(1.0, value)) * 32767))
            try:
                self._player.stdin.write(samples.tobytes())
            except BrokenPipeError:
                return

    def close(self) -> None:
        self._player.terminate()


def colorize(row: str) -> str:
    out = []
    for char in row:
        level = LEVELS.find(char)
        out.append(char if level < 0 else f"\033[48;5;{COLORS[level]}m \033[0m")
    return "".join(out)


def parse_peak(tail: str) -> int | None:
    # tail looks like " peak 2437 MHz (13%)"
    parts = tail.split()
    if len(parts) >= 2 and parts[0] == "peak" and parts[1].isdigit():
        return int(parts[1])
    return None


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = args[0] if args else "/dev/ttyUSB0"
    sound = None
    if "--mute" not in sys.argv:
        if shutil.which("pacat"):
            sound = RadioSound()
        else:
            print("pacat not found: running without sound")
    try:
        with serial.Serial(port, 115200, timeout=1) as link:
            print(f"Listening on {port} (Ctrl+C to quit)")
            while True:
                line = link.readline().decode(errors="replace").rstrip("\r\n")
                if line.startswith("    |") and line.count("|") >= 2:
                    start = line.index("|") + 1
                    end = line.index("|", start)
                    row, tail = line[start:end], line[end + 1:]
                    if sound:
                        levels = [max(0, LEVELS.find(c)) for c in row]
                        sound.update(levels, parse_peak(tail))
                    print("    |" + colorize(row) + "|" + tail + RESET, flush=True)
                elif line:
                    print(line, flush=True)
    finally:
        if sound:
            sound.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(RESET)
