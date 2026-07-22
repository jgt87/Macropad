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

How it works — **two independent layers per key**, shown as the *Sends* and *Runs macro* columns:

1. **Sends (device):** what the physical key is programmed to emit — a real combo like `Ctrl+C`,
   or a *trigger key* `F13`–`F24` (no keyboard has these, so nothing else ever sends them). Set
   with **Bind to key**; written to the pad's flash, so it works on any PC.
2. **Runs macro (app):** the recorded macro the app plays when it *detects* that sent key. Set
   with **Map Macro**. So: press the pad key → it sends e.g. `F13` → the running app catches
   `F13` and replays the macro. This is what gets past the 5-keystroke limit and adds app-context.

- **Run it, either way:**
  - **Packaged (no Python):** grab `MacroStudio.exe` (from a release, or build it — see below)
    and double-click it. It's a single portable file; its `macros.json` config lives next to it.
  - **From source:** `pip install hidapi keyboard pystray Pillow`, then `pythonw macro_studio.py`
    (auto-starts via a Startup-folder shortcut).
- **Workflow:** Record New → select the macro + a key → **Map Macro**. That auto-assigns the key
  its default trigger (`F13`–`F21`) and programs the pad, then links the macro. Press the key to
  run it. Use **Test** to try without the pad. Closing the window hides it to the tray.
- **App context is automatic.** While recording, click into the application you want the
  macro to run in — Macro Studio notes which process had focus when your first keystroke
  landed and binds the macro to that executable. At playback it focuses that app first, and
  **starts it if it isn't running**, waiting for its window to appear (up to 30s) before
  typing. If the app can't be started the macro is skipped rather than typed into whatever
  happened to be focused. **Set App…** overrides the detection by hand.
- **Config:** `macros.json`. **Smoke-test:** `python macro_studio.py --selftest`.
- Notes: keystroke macros only (no mouse); global hooks may need the app run as admin to
  catch every key.

## Bind a key to a keyboard combination

Select a key and click **Bind to key**, then either press the combination (e.g. `Ctrl+Shift+S`,
or a short sequence like `Ctrl+C Ctrl+V`) **or** pick a trigger key `F13`–`F24` from the list
(those have no physical key, so you can't press them). Macro Studio programs it **straight onto
the pad's flash**, so the key sends it on **any** PC with no app running — it travels with the
device. Use a real combo when you want the key to *do* that thing everywhere; use an `F13`+
trigger when you want it to drive a macro (then link one with **Map Macro** — the two coexist).
**Unbind** clears the key.

## Profiles & app-bound auto-switching

A **profile** is a named set of key→macro mappings. Your recorded macros are shared across all
profiles; a profile just decides *which* macro each key runs — so one pad can do different things
in different contexts. Switch profiles from the **Profile ▾** dropdown at the top-right, or let
Macro Studio switch them **automatically based on the app you're using**.

**Set it up (all from the Profile ▾ menu):**

1. Make a profile per context — **New profile…**, then map keys in it with **Map Macro** as usual.
2. Tie a profile to an app: focus the target app once (e.g. VS Code), come back to Macro Studio,
   open **Profile ▾** and click **Assign current app → '‹profile›'**. The menu then shows the
   profile with its app, e.g. `VS Code — code.exe`. (An app belongs to one profile; assigning it
   elsewhere moves it.)
3. Pick a fallback with **Set '‹profile›' as fallback** — used when the focused app isn't tied to
   any profile. It defaults to `Default` and is marked `— fallback` in the menu.
4. Turn on **Auto-switch by app** (a ✓ appears next to it).

Now focusing a tied app activates its profile; focusing an app tied to nothing activates the
fallback. Focusing Macro Studio itself (or the desktop) leaves the current profile alone.

**Good to know:**

- Switching is **instant and never touches the device** — every profile uses the same pad
  triggers (`F13`–`F21`), so only the app-side macro mapping changes. Nothing is written to flash,
  so there's no wear or lag when you tab between apps.
- It **pauses while recording** or while a dialog is open, so it can't fire mid-capture.
- The one exception: if a profile changed what a key physically *sends* via **Bind to key** (a real
  combo lives in the pad's flash), that part can't change per-app on its own — click **Reprogram
  device** after switching to push that profile's `Sends` values to the pad. Trigger-based profiles
  (the **Map Macro** default) switch with no extra step.

## Moving binds to another PC

The firmware can't hold Macro Studio's *macros* — it's **write-only** (nothing can read a bind
back to show it elsewhere) and a key stores at most **5 keystrokes** with no timing, mouse, or
app focus. So macros travel with the *config*, two ways:

- **Carry the portable copy.** `macros.json` lives next to `MacroStudio.exe`. Put that folder
  on a USB stick (or copy it to the other PC) and your binds show up there. The tray menu's
  **Open config folder** opens exactly this folder.
- **Export / Import.** **Export…** writes all macros + bindings to one `.json`; **Import…**
  loads it on another install (then use **Map Macro** to arm the physical keys there).

(Simple combos set with **Bind to key** don't need any of this — they're written to the
device's own flash, so they already travel with the pad to any PC on their own.)

## Building the executable

From `custom/`, run `powershell -ExecutionPolicy Bypass -File build.ps1` to produce
`dist/MacroStudio.exe`. Pushing a `vX.Y.Z` tag makes GitHub Actions build the app and
attach `MacroStudio-vX.Y.Z.exe` to the release.

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
