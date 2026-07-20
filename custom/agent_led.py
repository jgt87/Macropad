#!/usr/bin/env python3
"""
agent_led.py - drive the macropad LED as an "agent is busy" indicator.

Usage:
    python agent_led.py on      # agent is working / busy -> color-cycle
    python agent_led.py off      # agent is done / idle    -> LEDs off

Wired into Claude Code (settings.json hooks): the cycle starts when you submit a prompt
(UserPromptSubmit) and stops when the agent finishes its turn (Stop). All errors are
swallowed on purpose: if the macropad is unplugged or busy, the agent must never be
disturbed by this indicator.

Note: changing the LED writes to the device flash (the firmware ignores no-save writes),
so this fires ~once when a turn starts and once when it ends -- not a constant loop.
"""
import os
import sys

CYCLE_MODE = 2   # color-cycle chase (reliably visible from any state) = "busy"
OFF_MODE = 0     # all LEDs off = "done / idle"

ON_WORDS = {"on", "busy", "working", "cycle", "start"}

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    arg = (sys.argv[1] if len(sys.argv) > 1 else "on").lower()
    mode = CYCLE_MODE if arg in ON_WORDS else OFF_MODE
    try:
        import macropad as m
        with m.Macropad() as mp:
            mp.set_led(mode)
    except Exception:
        pass  # never fail the hook


if __name__ == "__main__":
    main()
