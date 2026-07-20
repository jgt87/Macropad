# Macropad

Open-source controller for the **MINI KeyBoard** macro keyboard (USB **VID `0x1189` / PID
`0x8890`**) — the little 6-key + 1-knob pad that ships with a Windows-only vendor app.

The vendor app configures the device over a plain, undocumented HID protocol. That protocol has
been recovered and reimplemented in Python, so you can program the pad from the command line,
from your own scripts, and without keeping any software running: assignments are written to the
device's flash and travel with it to any computer.

* **[`custom/README.md`](custom/README.md)** — install, usage, and the companion tray app.
* **[`custom/PROTOCOL.md`](custom/PROTOCOL.md)** — the full HID protocol: frame layout, key
  numbers, keyboard/media/mouse/LED commands, and what the firmware does *not* support.

```bash
pip install hidapi
cd custom
python macropad.py list                 # confirm the device is found
python macropad.py key 1 ctrl+c         # key 1 -> Ctrl+C, saved to flash
python macropad.py --dry-run key 1 f5   # preview the HID frames, send nothing
```

Every command accepts `--dry-run`, which prints the exact bytes instead of touching hardware.

## What's here

| | |
|---|---|
| `custom/macropad.py` | The controller — Python library **and** CLI. |
| `custom/macro_studio.py` | Tray app: record, bind, and run macros of any length. |
| `custom/PROTOCOL.md` | The reverse-engineered HID protocol, written up in full. |
| `custom/layout.json` | Your layout as a file — the device can't report its own config. |

## Notes

The vendor's `MINI KeyBoard.exe` and its decompilation are **not** included — that binary isn't
ours to redistribute. `PROTOCOL.md` records everything learned from it, which is all you need.

Tested against the Report ID 3 firmware on a 6-key + 1-knob unit. The protocol also covers the
vendor's larger models (up to 12 keys and 3 knobs); those paths are implemented but untested.

Not affiliated with or endorsed by the device's manufacturer.
