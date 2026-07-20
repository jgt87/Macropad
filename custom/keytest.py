"""Foreground trigger check: run this in your own terminal, then press macropad keys.

    python keytest.py

Prints every key event and flags the F13..F21 macro triggers. Because it runs in your
interactive session (not detached), the low-level hook receives real hardware input.
Press Esc to quit. If pressing a macropad key prints nothing here, the device is not
emitting - reprogram it (Program key on device). If it prints the F-key but Macro Studio
still does nothing, the problem is in Macro Studio, not the device or the trigger."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keyboard
from macro_studio import CHORDS, KEY_LABELS

trigger = {v: KEY_LABELS[k] for k, v in CHORDS.items()}   # 'f13' -> 'Key 1'
print("Listening. Press your macropad keys (Esc to quit).")
print("Expecting:", ", ".join(f"{v}={KEY_LABELS[k]}" for k, v in CHORDS.items()))

def on_key(e):
    if e.event_type != "down":
        return
    mark = "  <<< MACRO TRIGGER: %s" % trigger[e.name] if e.name in trigger else ""
    print(f"  {e.name!r:12} scan={e.scan_code}{mark}")

keyboard.on_press(on_key)
keyboard.wait("esc")
print("done")
