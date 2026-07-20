# Custom software for the MINI KeyBoard macropad (VID 1189 / PID 8890)

Your own, open replacement for the bundled `MINI KeyBoard.exe`. It talks to the macropad
over the exact same HID protocol as the vendor app (recovered by decompiling that app),
so anything you assign is written to the device's flash and works everywhere — no
software needs to keep running.

## What's in this folder

| File | What it is |
|------|-----------|
| `macropad.py` | The controller — a Python library **and** command-line tool. |
| `PROTOCOL.md` | Full write-up of the HID protocol (frames, key numbers, codes). |
| `macro_studio.py` | Tray app to record, bind, and run macros past the 5-keystroke limit. |

> The vendor app and its decompilation (`decompiled/FullSource.cs`) are **not** included in
> this repository — they're the vendor's binary, not ours to redistribute. Everything needed
> to talk to the device is in `macropad.py` and `PROTOCOL.md`.

## Answering the original question

* **"Can the software be decrypted?"** It isn't encrypted. `MINI KeyBoard.exe` is a plain
  .NET assembly (even ships with `.pdb` debug symbols), so it decompiles straight back to
  readable C# with any .NET decompiler.
* **"Can we write our own?"** Yes — that's `macropad.py`. It reimplements the protocol
  cleanly and is verified to produce byte-for-byte the same HID frames as the original.

## Setup

```
pip install hidapi
```

On Windows the config interface is usually accessible without extra drivers. If a
command reports a permission/open error, run the terminal as Administrator once.

## Usage

Everything is safe to inspect first: add `--dry-run` to **any** command to print the exact
HID frames without touching the device.

```bash
# Read-only: list the device's interfaces and confirm it's found
python macropad.py list

# Assign Ctrl+C to physical key 1
python macropad.py key 1 ctrl+c

# Assign a text macro "hi" to key 2 (one token per keystroke)
python macropad.py key 2 h i

# A single function key, or a combo
python macropad.py key 3 f5
python macropad.py key 4 ctrl+shift+esc

# Multimedia on a knob press (key 14 = knob-1 press on this unit)
python macropad.py media 14 mute
python macropad.py media 13 voldown      # knob 1 turn-left
python macropad.py media 15 volup        # knob 1 turn-right

# Mouse actions
python macropad.py mouse 5 middle
python macropad.py mouse 6 wheeldown

# Backlight effect: 0=off, 1=steady white (one LED), 2=colour-cycle chase,
# 3=pulse/breathing frozen on the cycle's current colour
python macropad.py led 1
# To "pick" a colour: start the cycle, then freeze it when the colour you want appears
python macropad.py led 2   # ...watch...   then quickly:
python macropad.py led 3

# Preview the frames for anything without sending:
python macropad.py --dry-run key 1 ctrl+alt+del
```

Physical key numbering for this unit (6 keys + 1 knob; see `PROTOCOL.md` for the full
table): keys `1`–`6`; knob = `13` (turn one way) / `14` (press) / `15` (turn other way).
The protocol also supports larger models (keys up to 12, knobs at 13/14/15, 16/17/18,
19/20/21). If your hardware differs, assign a distinct macro to each number and watch
which input fires to map it.

## Using it as a library

```python
from macropad import Macropad, MOD_CTRL, KEYCODES, MEDIA_CODES

with Macropad() as mp:
    mp.set_keyboard(1, [(MOD_CTRL, KEYCODES["c"])])          # key 1 -> Ctrl+C
    mp.set_keyboard(2, [(0, KEYCODES["h"]), (0, KEYCODES["i"])])  # key 2 -> "hi"
    mp.set_multimedia(14, MEDIA_CODES["mute"])               # knob press -> mute
    mp.set_led(1)
```

## Known keys / codes

* **Keyboard:** letters, digits, `f1`–`f12`, `enter`, `esc`, `tab`, `space`, `backspace`,
  `del`, `home`, `end`, `pgup`, `pgdn`, `up`/`down`/`left`/`right`, and punctuation
  (see `KEYCODES` in `macropad.py`). Prefix with modifiers using `+`, e.g. `ctrl+shift+s`.
* **Media:** `play`, `pause`, `mute`, `volup`, `voldown`, `next`, `prev`, `stop`.
* **Mouse:** `left`, `right`, `middle`, `wheelup`, `wheeldown`.

## Macro Studio (main app) — record, bind, and run macros

`macro_studio.py` is the primary companion app: a system-tray program to **record**
keystroke macros, **bind** them to macropad keys, and **run** them — with optional
application context (launch/focus an app first). This gets past the 5-keystroke limit and
lets one key do long/app-aware actions.

How it works: each key sends a unique chord it listens for — **Keys 1–6 → Ctrl+Alt+Win+F1–F6**,
**knob 13/14/15 → F7–F9** (these never collide with normal shortcuts).

- **Install deps** (one-time): `pip install hidapi keyboard pystray Pillow`
- **Run:** `pythonw macro_studio.py` (auto-starts via a Startup-folder shortcut).
- **Workflow:** Record New → select macro + key → Bind →Key → Program Key on Device. Then
  press that macropad key to run the macro. Use **Test** to try without the pad. Closing the
  window hides it to the tray.
- **App context is automatic.** While recording, click into the application you want the
  macro to run in — Macro Studio notes which process had focus when your first keystroke
  landed and binds the macro to that executable. At playback it focuses that app first, and
  **starts it if it isn't running**, waiting for its window to appear (up to 30s) before
  typing. If the app can't be started the macro is skipped rather than typed into whatever
  happened to be focused. **Set App…** overrides the detection by hand.
- **Config:** `macros.json`. **Smoke-test:** `python macro_studio.py --selftest`.
- Notes: keystroke macros only (no mouse); global hooks may need the app run as admin to
  catch every key.

### Legacy: Macropad Helper (AutoHotkey) — retired

`macropad-helper.ahk` was an earlier, simpler pop-up-menu approach (trigger `Ctrl+Alt+Win+F12`).
It has been **superseded by Macro Studio** and is no longer running or auto-starting. The
file is kept for reference; delete it if you don't want it. To revive it: run the `.ahk`
with AutoHotkey v2 and bind a key with `python macropad.py key <N> ctrl+alt+win+f12`.

## Caveats

* Writing an assignment **overwrites** whatever that key/knob currently does and stores it
  to the device's flash. There is no "read current config" command in the protocol, so
  keep your own notes if you want to revert (or just reassign).
* Max 5 keystrokes per macro (a firmware/app limit).
* Tested against Report ID 3 firmware (what this device reports). The vendor app also
  handled older Report ID 0 / 2 variants; if you ever see a different unit, check
  `list` and the notes in `PROTOCOL.md`.
