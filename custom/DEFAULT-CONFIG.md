# Default configuration — MINI KeyBoard (VID 0x1189 / PID 0x8890)

Factory default bindings for this **6-key + 1-knob** macropad (6 LEDs, one per key),
discovered by observation on **2026-07-17** (the firmware has no "read config" command, so
these were mapped by pressing each input and watching the result).

This is the **restore point**. The machine-readable copy is
[`default-config.json`](default-config.json) — apply it to reset the device to factory:

```
python macropad.py apply default-config.json      # add --dry-run to preview first
```

For everyday changes, edit [`layout.json`](layout.json) instead and keep this file pristine.

## Key bindings

| # | Input | Binding | Keystroke / code |
|---|-------|---------|------------------|
| 1 | Key 1 | Copy | `Ctrl+C` |
| 2 | Key 2 | Paste | `Ctrl+V` |
| 3 | Key 3 | Windows Emoji picker | `Win+.` |
| 4 | Key 4 | Screenshot / Snipping Tool | `Win+Shift+S` |
| 5 | Key 5 | Move window to other monitor | `Win+Shift+Right` |
| 6 | Key 6 | Task View | `Win+Tab` |
| 13 | Knob — turn left | Volume Down | media `voldown` (0xEA) |
| 14 | Knob — press | Switch window | `Alt+Tab` |
| 15 | Knob — turn right | Volume Up | media `volup` (0xE9) |

> Knob direction note: with two monitors, `Win+Shift+Left` and `Win+Shift+Right` both just
> toggle the window between screens, so Key 5's exact arrow can't be told apart by behavior;
> `Right` is recorded as the conventional choice.

## LED backlight (not part of key bindings)

The 6 RGB LEDs are controlled separately (no per-LED / color command exists in the firmware):

| `python macropad.py led N` | Effect |
|---|---|
| `led 0` | off |
| `led 1` | one LED steady, white |
| `led 2` | color-cycle chase across all 6 |
| `led 3` | pulse/breathing, frozen on the cycle's current color |

To land on a specific color: run `led 2`, wait for the color you want, then `led 3` to freeze it.
