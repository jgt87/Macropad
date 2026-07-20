#!/usr/bin/env python3
"""
macropad.py - Open-source controller for the VID:1189 PID:8890 mini macro keyboard
(the device shipped with the "MINI KeyBoard.exe" / "HIDTester" Windows app).

This is a clean-room reimplementation of the HID configuration protocol used by the
original vendor app, recovered by decompiling it. See PROTOCOL.md for the full spec.

Requires:  pip install hidapi

Nothing is written to the device unless you run a sub-command that assigns a key.
Use --dry-run on any command to print the exact HID frames without sending them.

Device layout (this unit: 6 keys + 1 knob; 6 LEDs, one per key):
    Keys .......... 1..6
    Knob .......... 13 = turn one way,  14 = press,  15 = turn other way
(The vendor protocol also supports larger models: keys 1..12 and up to 3 knobs at
 13/14/15, 16/17/18, 19/20/21. If your unit differs, assign a distinct macro per number
 and watch which input fires.)
"""

import argparse
import sys
import time

try:
    import hid
except ImportError:
    sys.exit("The 'hidapi' package is required.  Install it with:  pip install hidapi")

VID = 0x1189
PID = 0x8890
CONFIG_INTERFACE_TAG = b"mi_01"   # the vendor-defined HID interface used for config
REPORT_ID = 3                     # only output report the device defines
OUTPUT_DATA_LEN = 64              # data bytes after the report id (report is 65 bytes total)

# --- Payload field offsets (index into the 8 meaningful data bytes) -----------------
# Byte 0: physical key number (or special: 0xA1 layer-switch, 0xAA save, 0xB0 LED)
# Byte 1: (layer << 4) | type   where type nibble is one of the KEYTYPE_* below
# Bytes 2..7: type-specific

KEYTYPE_KEYBOARD   = 1   # normal key / multi-key macro (up to 5 keystrokes)
KEYTYPE_MULTIMEDIA = 2   # consumer-control key (media / volume)
KEYTYPE_MOUSE      = 3   # mouse button / wheel
KEYTYPE_LED        = 8   # backlight mode

# Modifier bitmask (standard USB HID, left-side)
MOD_CTRL  = 0x01
MOD_SHIFT = 0x02
MOD_ALT   = 0x04
MOD_WIN   = 0x08
MODIFIERS = {"ctrl": MOD_CTRL, "control": MOD_CTRL,
             "shift": MOD_SHIFT,
             "alt": MOD_ALT,
             "win": MOD_WIN, "gui": MOD_WIN, "meta": MOD_WIN, "cmd": MOD_WIN}

# --- USB HID Keyboard/Keypad usage codes (page 0x07) --------------------------------
KEYCODES = {}
for i, c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    KEYCODES[c] = 0x04 + i
for i, c in enumerate("1234567890"):
    KEYCODES[c] = 0x1E + i
KEYCODES.update({
    "enter": 0x28, "return": 0x28, "esc": 0x29, "escape": 0x29,
    "backspace": 0x2A, "bksp": 0x2A, "tab": 0x2B, "space": 0x2C, "spacebar": 0x2C,
    "minus": 0x2D, "-": 0x2D, "equal": 0x2E, "=": 0x2E,
    "lbracket": 0x2F, "[": 0x2F, "rbracket": 0x30, "]": 0x30,
    "backslash": 0x31, "\\": 0x31, "semicolon": 0x33, ";": 0x33,
    "quote": 0x34, "'": 0x34, "grave": 0x35, "`": 0x35,
    "comma": 0x36, ",": 0x36, "dot": 0x37, "period": 0x37, ".": 0x37,
    "slash": 0x38, "/": 0x38, "capslock": 0x39,
    "printscreen": 0x46, "prtsc": 0x46, "scrolllock": 0x47, "pause": 0x48,
    "insert": 0x49, "ins": 0x49, "home": 0x4A, "pageup": 0x4B, "pgup": 0x4B,
    "delete": 0x4C, "del": 0x4C, "end": 0x4D, "pagedown": 0x4E, "pgdn": 0x4E,
    "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52,
})
for n in range(1, 13):
    KEYCODES["f%d" % n] = 0x3A + (n - 1)   # F1..F12 = 0x3A..0x45

# --- Multimedia (Consumer Control) codes for this firmware (ReportID 3) --------------
MEDIA_CODES = {
    "play": 0xCD, "playpause": 0xCD, "pause": 0xCD,
    "prev": 0xB6, "previous": 0xB6, "prevtrack": 0xB6,
    "next": 0xB5, "nexttrack": 0xB5,
    "mute": 0xE2,
    "volup": 0xE9, "volumeup": 0xE9, "vol+": 0xE9,
    "voldown": 0xEA, "volumedown": 0xEA, "vol-": 0xEA,
    "stop": 0xB7,
}

# --- Mouse buttons -------------------------------------------------------------------
MOUSE_BUTTONS = {"left": 0x01, "right": 0x02, "middle": 0x04, "centre": 0x04, "center": 0x04}


class Macropad:
    def __init__(self, dry_run=False, verbose=False):
        self.dry_run = dry_run
        self.verbose = verbose or dry_run
        self.dev = None
        self.path = None

    # -- connection --------------------------------------------------------------
    @staticmethod
    def find_path():
        for d in hid.enumerate(VID, PID):
            if CONFIG_INTERFACE_TAG in d["path"].lower():
                return d["path"]
        return None

    def open(self):
        if self.dry_run:
            return self
        self.path = self.find_path()
        if not self.path:
            raise RuntimeError(
                "Macropad config interface (VID 1189 / PID 8890, interface mi_01) not found. "
                "Is the device plugged in?")
        self.dev = hid.device()
        self.dev.open_path(self.path)
        return self

    def close(self):
        if self.dev:
            self.dev.close()
            self.dev = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    # -- raw frame ---------------------------------------------------------------
    def _send(self, data8):
        """data8 = list/bytes of up to 8 payload bytes. Builds and sends the 65-byte report."""
        payload = list(data8) + [0] * (8 - len(data8))
        report = [REPORT_ID] + payload + [0] * (OUTPUT_DATA_LEN - 8)  # 1 + 64 = 65 bytes
        if self.verbose:
            shown = " ".join("%02X" % b for b in report[:10])
            print("  TX  id=%d  data=[%s ...]" % (REPORT_ID, shown[3:]))
        if not self.dry_run:
            n = self.dev.write(report)
            if n < 0:
                raise IOError("HID write failed")
            time.sleep(0.01)  # give the MCU time to store to flash

    # -- protocol primitives -----------------------------------------------------
    def switch_layer(self, layer):
        self._send([0xA1, layer if layer else 1])

    def _save(self):
        self._send([0xAA, 0xAA])

    def _save_led(self):
        self._send([0xAA, 0xA1])

    def _type_byte(self, layer, keytype):
        return ((layer & 0x0F) << 4) | (keytype & 0x0F)

    # -- high level assignment ---------------------------------------------------
    def set_keyboard(self, key, keystrokes, layer=1):
        """keystrokes = list of (modifier_mask, hid_keycode). Max 5. keycode 0 = modifier-only."""
        if not 1 <= len(keystrokes) <= 5:
            raise ValueError("a keyboard macro must have 1..5 keystrokes")
        self.switch_layer(layer)
        count = len(keystrokes)
        t = self._type_byte(layer, KEYTYPE_KEYBOARD)
        mod0 = keystrokes[0][0]
        # b == 0 : header report (first modifier, keycode 0)
        self._send([key, t, count, 0, mod0, 0])
        # b == 1..count : one report per keystroke
        for b, (mod, code) in enumerate(keystrokes, start=1):
            self._send([key, t, count, b, mod, code])
        self._save()

    def set_multimedia(self, key, code, layer=1):
        self.switch_layer(layer)
        t = self._type_byte(layer, KEYTYPE_MULTIMEDIA)
        self._send([key, t, code, 0])
        self._save()

    def set_mouse(self, key, buttons=0, wheel=0, modifier=0, layer=1):
        self.switch_layer(layer)
        t = self._type_byte(layer, KEYTYPE_MOUSE)
        # payload: [key, type, buttons, 0, 0, wheel, modifier, 0]
        self._send([key, t, buttons & 0xFF, 0, 0, wheel & 0xFF, modifier & 0xFF])
        self._save()

    def set_led(self, mode, layer=1):
        t = self._type_byte(layer, KEYTYPE_LED)
        self._send([0xB0, t, mode & 0xFF])
        self._save_led()


# ---------------------------------------------------------------------------------
# Argument parsing helpers
# ---------------------------------------------------------------------------------
def parse_keystroke(token):
    """'ctrl+shift+s' -> (MOD_CTRL|MOD_SHIFT, keycode('s')).  'a' -> (0, keycode('a'))."""
    parts = [p for p in token.lower().split("+") if p != ""]
    mod = 0
    key = None
    for p in parts:
        if p in MODIFIERS:
            mod |= MODIFIERS[p]
        else:
            if p not in KEYCODES:
                raise SystemExit("Unknown key name: %r (try letters, digits, f1..f12, "
                                 "enter, esc, tab, space, arrows, home, del, ...)" % p)
            key = KEYCODES[p]
    if key is None:
        raise SystemExit("Keystroke %r has modifiers but no base key" % token)
    return (mod, key)


def cmd_list(args):
    found = False
    for d in hid.enumerate(VID, PID):
        found = True
        tag = "  <-- CONFIG CHANNEL" if CONFIG_INTERFACE_TAG in d["path"].lower() else ""
        print("interface %d  usage_page=0x%04x usage=0x%02x%s" %
              (d["interface_number"], d["usage_page"], d["usage"], tag))
        print("    path:", d["path"].decode(errors="replace"))
    if not found:
        print("No VID:1189 PID:8890 device found. Is it plugged in?")


def cmd_key(args):
    keystrokes = [parse_keystroke(t) for t in args.keys]
    with Macropad(dry_run=args.dry_run) as mp:
        mp.set_keyboard(args.keynum, keystrokes, layer=args.layer)
    print("Assigned %d keystroke(s) to key %d (layer %d)%s"
          % (len(keystrokes), args.keynum, args.layer, dry(args)))


def cmd_media(args):
    name = args.name.lower()
    if name not in MEDIA_CODES:
        raise SystemExit("Unknown media key. Options: " + ", ".join(sorted(MEDIA_CODES)))
    with Macropad(dry_run=args.dry_run) as mp:
        mp.set_multimedia(args.keynum, MEDIA_CODES[name], layer=args.layer)
    print("Assigned media '%s' to key %d (layer %d)%s"
          % (name, args.keynum, args.layer, dry(args)))


def cmd_mouse(args):
    buttons = 0
    wheel = 0
    modifier = 0
    a = args.action.lower()
    if a in MOUSE_BUTTONS:
        buttons = MOUSE_BUTTONS[a]
    elif a in ("wheelup", "wheel+", "scrollup"):
        wheel = 0x01
    elif a in ("wheeldown", "wheel-", "scrolldown"):
        wheel = 0xFF
    else:
        raise SystemExit("Unknown mouse action. Options: left, right, middle, "
                         "wheelup, wheeldown")
    with Macropad(dry_run=args.dry_run) as mp:
        mp.set_mouse(args.keynum, buttons=buttons, wheel=wheel, modifier=modifier,
                     layer=args.layer)
    print("Assigned mouse '%s' to key %d (layer %d)%s"
          % (a, args.keynum, args.layer, dry(args)))


def _mouse_from_action(a):
    a = a.lower()
    if a in MOUSE_BUTTONS:
        return dict(buttons=MOUSE_BUTTONS[a])
    if a in ("wheelup", "wheel+", "scrollup"):
        return dict(wheel=0x01)
    if a in ("wheeldown", "wheel-", "scrolldown"):
        return dict(wheel=0xFF)
    raise SystemExit("Unknown mouse action %r (left, right, middle, wheelup, wheeldown)" % a)


def cmd_apply(args):
    import json
    with open(args.file, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    layer = cfg.get("layer", args.layer)
    keys = cfg.get("keys", {})
    written = skipped = 0
    with Macropad(dry_run=args.dry_run) as mp:
        for num in sorted(keys, key=lambda k: int(k)):
            entry = keys[num]
            knum = int(num)
            typ = entry.get("type", "key")
            note = entry.get("note", "")
            if typ == "key":
                spec = entry.get("keys", "")
                tokens = spec.split() if isinstance(spec, str) else list(spec)
                if not tokens:
                    skipped += 1
                    continue  # empty = leave that key untouched
                ks = [parse_keystroke(t) for t in tokens]
                mp.set_keyboard(knum, ks, layer=layer)
            elif typ == "media":
                name = entry["name"].lower()
                if name not in MEDIA_CODES:
                    raise SystemExit("key %s: unknown media %r" % (num, name))
                mp.set_multimedia(knum, MEDIA_CODES[name], layer=layer)
            elif typ == "mouse":
                mp.set_mouse(knum, layer=layer, **_mouse_from_action(entry["action"]))
            elif typ == "led":
                mp.set_led(int(entry["mode"]))
            else:
                raise SystemExit("key %s: unknown type %r" % (num, typ))
            written += 1
            print("  key %-2s <- %-6s %-14s %s" % (num, typ, entry.get("keys", entry.get("name", entry.get("action", entry.get("mode", "")))), ("# " + note) if note else ""))
    print("Applied %d assignment(s), skipped %d empty (layer %d)%s"
          % (written, skipped, layer, dry(args)))


def cmd_led(args):
    with Macropad(dry_run=args.dry_run) as mp:
        mp.set_led(args.mode)
    print("Set LED mode %d%s" % (args.mode, dry(args)))


def dry(args):
    return "  [DRY RUN - nothing sent]" if args.dry_run else ""


def build_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="print the HID frames instead of sending them")
    p.add_argument("--layer", type=int, default=1, help="target layer (default 1)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list", help="list the device's HID interfaces (read-only)")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("key", help="assign a key or multi-key macro to a physical key")
    s.add_argument("keynum", type=int, help="physical key number (see layout above)")
    s.add_argument("keys", nargs="+",
                   help="one token per keystroke, e.g.  ctrl+c   |   h e l l o   |   ctrl+shift+esc")
    s.set_defaults(func=cmd_key)

    s = sub.add_parser("media", help="assign a multimedia key")
    s.add_argument("keynum", type=int)
    s.add_argument("name", help="play, mute, volup, voldown, next, prev, stop")
    s.set_defaults(func=cmd_media)

    s = sub.add_parser("mouse", help="assign a mouse action")
    s.add_argument("keynum", type=int)
    s.add_argument("action", help="left, right, middle, wheelup, wheeldown")
    s.set_defaults(func=cmd_mouse)

    s = sub.add_parser("led", help="set backlight effect (0=off,1=steady,2=cycle,3=pulse)")
    s.add_argument("mode", type=int)
    s.set_defaults(func=cmd_led)

    s = sub.add_parser("apply", help="write a whole layout from a JSON file (your record)")
    s.add_argument("file", help="path to a layout .json (see layout.json)")
    s.set_defaults(func=cmd_apply)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
