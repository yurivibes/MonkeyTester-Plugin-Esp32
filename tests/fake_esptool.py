#!/usr/bin/env python3
"""Stands in for esptool in tests: checks the argv shape, records it, and exits
with FAKE_ESPTOOL_RC (default 0)."""

import json
import os
import sys

argv = sys.argv[1:]
log = os.environ.get("FAKE_ESPTOOL_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(argv) + "\n")
print("esptool.py v4.8.1")
print("Connecting....")
assert "write_flash" in argv, argv
i = argv.index("write_flash")
rest = argv[i + 1:]
pairs = []
j = 0
while j < len(rest):
    if rest[j].startswith("--"):
        j += 1 if rest[j] == "--erase-all" else 2
        continue
    pairs.append(rest[j])
    j += 1
for addr, path in zip(pairs[::2], pairs[1::2]):
    assert os.path.isfile(path), path
    print(f"Writing at {addr}... (100 %)")
    print(f"Wrote {os.path.getsize(path)} bytes at {addr}")
print("Hard resetting via RTS pin...")
sys.exit(int(os.environ.get("FAKE_ESPTOOL_RC", "0")))
