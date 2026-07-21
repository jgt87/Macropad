# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Software for a **MINI KeyBoard macropad, VID `0x1189` / PID `0x8890`** — this physical unit has
**6 keys + 1 knob** (6 RGB LEDs). `custom/` holds an open-source Python replacement for the
vendor app, written by decompiling it.

Work happens in `custom/`. The vendor binaries (`MINI KeyBoard.exe` and friends in the repo
root) and `custom/decompiled/FullSource.cs` (5.3k lines of decompiled C#) are present in the
working tree but **deliberately untracked** — see `.gitignore`. They are not redistributed;
only the independent reimplementation is published. `FullSource.cs` remains the local source of
truth whenever protocol behaviour is in question, so don't assume a fresh clone has it.

## Commands

All commands run from `custom/`. There is no automated test suite.

```bash
pip install hidapi                              # macropad.py
pip install hidapi keyboard pystray Pillow      # macro_studio.py

python macropad.py list                         # read-only: confirm device + config interface
python macropad.py key 1 ctrl+c                 # assign a key
python macropad.py apply layout.json            # write the whole layout
python macropad.py led 2                        # backlight effect
python macropad.py --dry-run <any subcommand>   # print HID frames, send nothing

pythonw macro_studio.py                         # tray GUI (auto-starts via Startup shortcut)
python macro_studio.py --selftest               # smoke test: config, bindings, hotkeys
powershell -ExecutionPolicy Bypass -File build.ps1  # build dist/MacroStudio.exe
```

Releases are built by `.github/workflows/release.yml` when a `v*` tag is pushed.

`--dry-run` works on every `macropad.py` subcommand and is the only way to verify frame
construction without hardware. Use it when changing protocol code.

## Architecture

**`macropad.py`** — library + CLI, and the only code that touches the device. `Macropad._send()`
builds every 65-byte output report (`[0x03] + 8 meaningful bytes + zero padding`); everything
else is a thin layer over it. Assignments are **write-only**: the firmware has no "read current
config" command, so the JSON files *are* the record of what's on the device.

**`macro_studio.py`** — the main user-facing app, and the workaround for the firmware's
**5-keystroke-per-macro limit**. Each macropad key is programmed to emit a unique chord
(keys 1–6 → `Ctrl+Alt+Win+F1–F6`, knob 13/14/15 → `F7–F9`); the tray app listens for those
chords with the `keyboard` library and replays a recorded macro of arbitrary length. It imports
`macropad` to program the device — it never speaks HID itself. State lives in `macros.json`
(gitignored/untracked; created on first run).

Two kinds of key assignment: a **chord-trigger binding** (`cfg["bindings"]`, keynum→macro; the
key sends F13–F21 and the app replays a rich macro) and a **native shortcut** (`cfg["shortcuts"]`,
keynum→`"ctrl+shift+s"`; the combo is written straight to firmware so the key sends it on any PC
with no app). They're mutually exclusive per key. **Assign shortcut** captures a combo and
programs it natively; `macro_as_keystrokes()` reduces recorded events (or a live capture) to
`(modifier, keycode)` chords and powers both **Assign shortcut** and **Send to device**.

Rich macros can't live on the device (write-only firmware — no read-back — and the 5-keystroke
limit), so they travel with the config instead: `export_bundle`/`import_bundle` (Export/Import
buttons) move macros+bindings+shortcuts as one JSON, and the portable `macros.json` sits beside
the `.exe`.

Its win32 layer (`ctypes` over `user32`/`kernel32`) does the app-context work:
`ForegroundWatcher` samples the foreground window while recording and `app_at()` picks the app
that had focus at the first keystroke; `ensure_app()` then focuses that app at playback —
preferring the exact recorded pid, falling back to any window of the same exe, and finally cold
-starting it and **polling for its window** rather than trusting a fixed delay. When resolving
an app fails, playback is abandoned: typing a macro into an arbitrary focused window is worse
than doing nothing. Set explicit `restype`/`argtypes` on any new user32 call returning or
taking an `HWND` — ctypes' default `c_int` truncates handles on 64-bit.

**`agent_led.py`** — tiny Claude Code hook helper: `on` starts the LED colour-cycle, `off` turns
it off, wired to `UserPromptSubmit`/`Stop` in user settings. It swallows **all** exceptions by
design — this indicator must never break an agent turn.

### Configuration files

- `layout.json` — the working layout; edit this for everyday changes, then `apply` it.
- `default-config.json` + `DEFAULT-CONFIG.md` — the factory restore point. Keep pristine.
- Entries with an empty `keys` value are skipped by `apply`, leaving that key untouched.

## Protocol constraints to respect

`PROTOCOL.md` is the full spec; the essentials that constrain any change:

- Config goes over interface **`mi_01`** only (vendor-defined, usage page `0xFF00`). The other
  interfaces are the HID keyboards the OS sees.
- This firmware defines **Report ID 3** only. The vendor app also handled older ID 0 / ID 2
  variants; that path is deliberately not reimplemented.
- Every assignment is `switch_layer` → data reports → **save**. Save is `[0xAA, 0xAA]` for
  keyboard/media/mouse but `[0xAA, 0xA1]` for LED — do not unify these.
- Writes go to **on-board flash** and overwrite the previous binding irreversibly. There is a
  10 ms sleep after each write to let the MCU commit.
- **LEDs: mode byte only.** There is no colour, brightness, or per-LED command; modes are
  `0` off, `1` steady white, `2` colour-cycle, `3` freeze-current-colour. Putting RGB values in
  the unused bytes was tested and does not work. Arbitrary colour needs custom MCU firmware.
- Max 5 keystrokes per keyboard macro (firmware limit — hence Macro Studio).

## Retired

`macropad-helper.ahk` (AutoHotkey v2 pop-up menu) is superseded by Macro Studio and no longer
runs or auto-starts. Kept for reference only.
