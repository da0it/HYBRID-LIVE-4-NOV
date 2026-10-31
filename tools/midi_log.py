#!/usr/bin/env python3
"""Show only HybridElectro lines from Ableton's log, optionally following live."""

import argparse
import re
import sys
import time
from collections import OrderedDict
from pathlib import Path


CC = re.compile(r"HybridElectro MIDI CC channel=(\d+) cc=(\d+) value=(\d+)")
NOTE = re.compile(r"HybridElectroPads MIDI NOTE channel=(\d+) note=(\d+) value=(\d+) on")


def latest_log():
    logs = list((Path.home() / "Library" / "Preferences" / "Ableton").glob("Live */Log.txt"))
    if not logs:
        raise FileNotFoundError("Ableton Log.txt not found. Start Live, or pass --log /path/to/Log.txt")
    return max(logs, key=lambda path: path.stat().st_mtime)


def summary(lines):
    controls = OrderedDict()
    for line in lines:
        match = CC.search(line)
        if match:
            channel, cc, value = map(int, match.groups())
            key = ("CC", channel, cc)
            controls.setdefault(key, []).append(value)
        match = NOTE.search(line)
        if match:
            channel, note, value = map(int, match.groups())
            key = ("NOTE", channel, note)
            controls.setdefault(key, []).append(value)
    for (kind, channel, number), values in controls.items():
        print("{} channel={} number={} values={}".format(kind, channel, number, values[-32:]))
    return controls


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--follow", action="store_true", help="Wait for NEW messages; Ctrl+C to stop")
    parser.add_argument("--summary", action="store_true", help="Group recorded CC/note values")
    args = parser.parse_args()
    if args.follow and args.summary:
        parser.error("Choose either --follow or --summary")
    path = args.log or latest_log()
    print("Log: " + str(path), flush=True)
    with path.open(encoding="utf-8", errors="replace") as stream:
        if args.summary:
            summary(stream)
            return
        if args.follow:
            stream.seek(0, 2)
            print("Turn the encoders / press selection pads. Ctrl+C stops this viewer.", flush=True)
        while True:
            line = stream.readline()
            if line:
                if "HybridElectro" in line:
                    print(line.rstrip(), flush=True)
            elif args.follow:
                if stream.tell() > path.stat().st_size:
                    stream.seek(0)
                time.sleep(0.1)
            else:
                break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
