#!/usr/bin/env python3
"""A stand-in for an installed console script, in the four shapes that matter.

``published_probe.watch`` closes stdin and gives the entrypoint a few seconds.
The four modes below are the outcomes it has to tell apart, and each one is a
real server behaviour rather than an invented one:

  announce   logs `server.start` and keeps running — a long-lived transport
  eof        logs `server.start` and exits 0 on EOF — a stdio server doing
             exactly what it should when stdin is closed
  crash      raises at start (parlament-mcp#29: the settings object went
             read-only and the process never came up)
  quiet      runs and announces nothing — neither a pass nor a crash
  flood      announces, then writes far more than a pipe buffer holds. A probe
             that waits for exit without draining deadlocks here.

Stdlib only, and no MCP: this fixture is about the process, not the protocol.
"""

from __future__ import annotations

import json
import sys
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else "announce"


def announce() -> None:
    print(json.dumps({"event": "server.start", "transport": "stdio"}), flush=True)


if MODE == "crash":
    print("starting", flush=True)
    raise ValueError('"Settings" object has no field "host"')

if MODE == "quiet":
    time.sleep(0.2)
    sys.exit(0)

announce()

if MODE == "eof":
    # A stdio server reads EOF from the closed stdin and shuts down cleanly.
    sys.stdin.read()
    sys.exit(0)

if MODE == "flood":
    for i in range(20_000):
        print(f"line {i} " + "x" * 60, flush=False)
    sys.stdout.flush()

# `announce` and `flood` stay up until the probe's window closes.
time.sleep(300)
