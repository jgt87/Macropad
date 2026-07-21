#!/usr/bin/env python3
"""
Macro Studio - record / bind / run macros for the VID:1189 PID:8890 macropad.

  * Record a keystroke macro (with timing). While recording, whichever application
    you click into is detected and remembered as the macro's target.
  * Bind it to a macropad key. The app assigns that key a unique "exotic" chord
    (Ctrl+Alt+Win+F1..F9) and programs the physical key to send it, then listens
    for that chord and replays the macro.
  * Application context per macro: the target app is focused before the macro runs,
    and started first if it isn't running, so the keystrokes land in the right program.
  * Lives in the system tray.

Requires:  pip install hidapi keyboard pystray Pillow      (Python 3.x, Windows)
Run:       python macro_studio.py         (add --selftest to smoke-test wiring)

Note: keystroke macros only (mouse movement is not recorded). Global keyboard hooks
sometimes need the app to run elevated to capture every key.
"""
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import keyboard

try:
    import mouse                      # companion to `keyboard`, same author
except ImportError:                   # keystroke-only mode; recording still works
    mouse = None

def _base_dir():
    """Writable base: the folder holding the .exe when frozen, else the script dir.

    In a PyInstaller onefile build __file__ points into a temp _MEIPASS dir that is
    deleted on exit, so state files (macros.json, the rewritten layout.json) must live
    next to the executable instead.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _bundle_dir():
    """Read-only bundled data dir (_MEIPASS when frozen, else the base dir)."""
    return getattr(sys, "_MEIPASS", _base_dir())


HERE = _base_dir()
BUNDLE_DIR = _bundle_dir()
sys.path.insert(0, HERE)
sys.path.insert(0, BUNDLE_DIR)
CONFIG_PATH = os.path.join(HERE, "macros.json")
LAYOUT_PATH = os.path.join(HERE, "layout.json")

try:
    from _version import __version__
except Exception:
    __version__ = "dev"

FOCUS_DELAY = 0.35      # settle time after focusing an app that was already running
LAUNCH_DELAY = 1.2      # extra settle time after a cold start (first paint is not "ready")
LAUNCH_TIMEOUT = 30.0   # how long to wait for a launched app's first window
LAUNCH_POLL = 0.25      # how often to look for that window
FOREGROUND_POLL = 0.2   # how often to sample the foreground window while recording
MAX_EVENT_GAP = 2.0     # cap replayed idle time: thinking pauses shouldn't be re-lived

# Each macropad key number -> its DEFAULT trigger key. Map Macro assigns this (and programs
# the pad to send it) when a key has no "sends" yet; Bind to key can override what a key sends.
#
# F13..F21 (single keys, no modifiers) rather than the old Ctrl+Alt+Win+Fn chords: those
# collided with reserved Windows shortcuts - Win+F1 opens Help, and Windows swallows the
# combo before any app's hook can see it, so the trigger never arrived. F13+ do not exist
# on a physical keyboard and no OS shortcut claims them, so they reach us intact and can
# never be pressed by accident.
CHORDS = {
    "1":  "f13", "2":  "f14", "3":  "f15", "4":  "f16", "5":  "f17", "6":  "f18",
    "13": "f19", "14": "f20", "15": "f21",
}
KEY_LABELS = {
    "1": "Key 1", "2": "Key 2", "3": "Key 3", "4": "Key 4", "5": "Key 5",
    "6": "Key 6", "13": "Knob turn left", "14": "Knob press", "15": "Knob turn right",
}
# display order of the key pane; list indices map back to key numbers through this
KEY_ORDER = ("1", "2", "3", "4", "5", "6", "13", "14", "15")
# The token macropad.py parses to program the device. Now identical to the listen form
# (single keys, no "windows" vs "win" difference), but kept as the one place that maps a
# key number to its device token, so a future chord change touches nothing else.
def chord_macropad_token(keynum):
    return CHORDS[keynum]


# ---------------------------------------------------------------- config
def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    else:
        cfg = {}
    cfg.setdefault("macros", {})      # name -> {"events": [...], "app": ""}
    cfg.setdefault("bindings", {})    # keynum -> macro name the app runs (layer 2)
    cfg.setdefault("shortcuts", {})   # keynum -> what the key sends, e.g. "f13" / "ctrl+c" (layer 1)
    # Migration: before the two layers were split, a mapped macro triggered on the fixed
    # CHORDS[keynum]. Record that as the key's "sends" so those binds keep firing.
    for keynum in cfg["bindings"]:
        if keynum in CHORDS:
            cfg["shortcuts"].setdefault(keynum, CHORDS[keynum])
    _migrate_macros(cfg)
    return cfg


def _migrate_macros(cfg):
    """Repair macros recorded before UWP apps were resolved.

    Those recorded ApplicationFrameHost.exe — the Store-app *host* — as their target. It
    cannot be launched, so playback would always fail; and the AUMID needed to fix them up
    is not recoverable from the stored path. Clearing the target degrades the macro to
    "run in the focused window" instead of failing outright, and Set App... can restore it."""
    for macro in cfg["macros"].values():
        if _normalise_exe(macro.get("app", "") or "nul") == APP_FRAME_HOST:
            macro["app"] = ""
            macro["app_pid"] = 0
        macro.setdefault("app_exe", "")


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def _replace_layout_line(keynum, entry_obj):
    """Rewrite layout.json's single-line entry for `keynum` to `entry_obj` (a dict).

    The firmware cannot report its own assignments, so layout.json is the only record of
    what is actually on the device. Any code that reprograms a key must update it here too,
    or the next `macropad.py apply layout.json` would quietly undo the change.

    The file is hand-maintained (aligned columns, per-key notes), so only the one entry's
    line is rewritten - the document is never re-dumped. Serialised in the file's own
    `{ "k": v, ... }` spacing. Every failure is reported, never raised: callers run this
    after a flash write has already succeeded, and a bookkeeping slip must not look like a
    failed operation."""
    if keynum not in CHORDS:
        return "layout.json not updated (unknown key %s)" % keynum
    try:
        with open(LAYOUT_PATH, "r", encoding="utf-8") as f:
            text = f.read()
        json.loads(text)                      # refuse to touch a file we can't parse
    except Exception as e:
        return "layout.json not updated (%s)" % e

    inner = ", ".join('"%s": %s' % (k, json.dumps(v)) for k, v in entry_obj.items())
    entry = "{ " + inner + " }"
    # matches a single-line entry, keeping its indent and the alignment after the colon
    pattern = re.compile(r'(?m)^(?P<pre>[ \t]*"%s"[ \t]*:[ \t]*)\{[^{}]*\}(?P<post>,?)[ \t]*$'
                         % re.escape(keynum))
    new_text, n = pattern.subn(lambda m: m.group("pre") + entry + m.group("post"), text, count=1)
    if not n:
        return "layout.json has no single-line entry for key %s - update it by hand" % keynum
    try:
        json.loads(new_text)                  # never write a file we just corrupted
    except Exception as e:
        return "layout.json not updated (edit would corrupt it: %s)" % e
    try:
        with open(LAYOUT_PATH, "w", encoding="utf-8") as f:
            f.write(new_text)
    except Exception as e:
        return "layout.json not updated (%s)" % e
    return "layout.json updated"


def record_in_layout(keynum, macro_name):
    """Point layout.json's entry for `keynum` at the trigger Bind just programmed."""
    return _replace_layout_line(keynum, {
        "type": "key", "keys": chord_macropad_token(keynum),
        "note": "Macro Studio: " + macro_name})


DEFAULT_CONFIG_PATH = os.path.join(HERE, "default-config.json")


def default_key_entry(keynum):
    """The factory entry for `keynum` from default-config.json, or None."""
    try:
        with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)["keys"].get(keynum)
    except Exception:
        return None


def default_key_desc(keynum):
    """A short human label for a key's factory function, e.g. 'Copy' or 'ctrl+c'."""
    e = default_key_entry(keynum) or {}
    return e.get("note") or e.get("keys") or e.get("name") or "its default"


def restore_key_default(keynum):
    """Program `keynum` back to its factory function and record it in layout.json.

    The firmware always emits *something*, so an unbound key can't be left silent - the
    honest alternative is to give it back the job it had before Macro Studio took it over,
    read from the read-only default-config.json. Returns (ok, message)."""
    entry = default_key_entry(keynum)
    if not entry:
        return False, "no factory default known for key %s" % keynum
    try:
        import macropad as m
        layer = 1
        typ = entry.get("type", "key")
        with m.Macropad() as mp:
            if typ == "key":
                ks = [m.parse_keystroke(t) for t in entry.get("keys", "").split()]
                if not ks:
                    return False, "default for key %s has no keystroke" % keynum
                mp.set_keyboard(int(keynum), ks, layer=layer)
            elif typ == "media":
                mp.set_multimedia(int(keynum), m.MEDIA_CODES[entry["name"].lower()], layer=layer)
            elif typ == "mouse":
                mp.set_mouse(int(keynum), layer=layer, **m._mouse_from_action(entry["action"]))
            else:
                return False, "unknown default type %r for key %s" % (typ, keynum)
    except Exception as e:
        return False, "device not reprogrammed (%s)" % e
    _replace_layout_line(keynum, entry)
    return True, "restored to %s" % default_key_desc(keynum)


# ---------------------------------------------------------------- portable binds
# The device can't hold Macro Studio's binds: the firmware is write-only (nothing can read a
# bind back to show it on another PC) and caps a key at five keystrokes with no timing, mouse,
# or app focus. So binds travel between machines two ways instead - a config file that carries
# everything (export/import, and the portable macros.json beside the .exe), and, for the few
# binds simple enough to fit, burning them onto the device as native keys that work anywhere
# with no app at all.
def export_bundle(path, cfg):
    """Write all macros + bindings + shortcuts to a file another PC's Macro Studio can import."""
    bundle = {"macro_studio_binds": 1,
              "macros": cfg.get("macros", {}),
              "bindings": cfg.get("bindings", {}),
              "shortcuts": cfg.get("shortcuts", {})}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2)


def import_bundle(path):
    """Read a file from export_bundle (or a raw macros.json) -> (macros, bindings, shortcuts)."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    macros = data.get("macros", {})
    bindings = data.get("bindings", {})
    shortcuts = data.get("shortcuts", {})
    if not (isinstance(macros, dict) and isinstance(bindings, dict)
            and isinstance(shortcuts, dict)):
        raise ValueError("not a Macro Studio binds file")
    return macros, bindings, shortcuts


# keyboard-library event names -> the tokens macropad.py understands. Only the spellings that
# differ need listing; letters, digits, f-keys, enter, etc. already match and pass through. An
# unknown name makes a macro ineligible for the device - it is never burned as a wrong guess.
_KB_MOD_ALIASES = {"windows": "win", "left windows": "win", "right windows": "win",
                   "left ctrl": "ctrl", "right ctrl": "ctrl", "control": "ctrl",
                   "left shift": "shift", "right shift": "shift",
                   "left alt": "alt", "right alt": "alt", "alt gr": "alt"}
_KB_KEY_ALIASES = {"page up": "pgup", "page down": "pgdn", "print screen": "printscreen",
                   "caps lock": "capslock", "scroll lock": "scrolllock"}


def macro_as_keystrokes(macro):
    """If `macro` fits the firmware, return the (modifier_mask, hid_keycode) chords to burn on
    the device; else None, so it stays a PC-side Macro Studio macro.

    A key holds at most five (modifier, key) chords and replays them on press - no timing, no
    mouse, no app focus. So only a short run of modified keystrokes, recorded with no app
    context, can live on the device and travel on its own. The event stream is reduced to
    chords: modifier presses accumulate a held mask, and each ordinary key-down emits one
    chord carrying whatever modifiers are down. Anything unrepresentable - a mouse event, an
    unknown key, a sixth keystroke - rejects the whole macro rather than burning an
    approximation onto irreversible flash."""
    try:
        import macropad as m
    except Exception:
        return None
    if macro.get("app"):
        return None
    strokes = []
    held = 0
    for ev in macro.get("events", []):
        if ev.get("src") != "k":
            return None
        name = (ev.get("n") or "").lower().strip()
        mod = m.MODIFIERS.get(_KB_MOD_ALIASES.get(name, name))
        if mod is not None:
            if ev.get("e") == "down":
                held |= mod
            elif ev.get("e") == "up":
                held &= ~mod
            continue
        if ev.get("e") != "down":
            continue
        code = m.KEYCODES.get(_KB_KEY_ALIASES.get(name, name).replace(" ", ""))
        if code is None:
            return None
        strokes.append((held, code))
        if len(strokes) > 5:
            return None
    return strokes or None


def keystroke_token(mod, code):
    """A (modifier, keycode) chord -> a 'ctrl+shift+s' token (for layout.json and display)."""
    import macropad as m
    names = {v: k for k, v in m.KEYCODES.items()}
    parts = [label for bit, label in ((m.MOD_CTRL, "ctrl"), (m.MOD_SHIFT, "shift"),
                                       (m.MOD_ALT, "alt"), (m.MOD_WIN, "win")) if mod & bit]
    parts.append(names.get(code, "0x%02x" % code))
    return "+".join(parts)


def keystrokes_text(strokes):
    """A chord list -> the space-separated token string macropad.py applies, 'ctrl+c ctrl+v'."""
    return " ".join(keystroke_token(mod, code) for mod, code in strokes)


# ---------------------------------------------------------------- win32 helpers (app context)
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_RESTORE = 9
SW_MINIMIZE = 6
SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001

# HWNDs are pointers: without these, ctypes' default c_int return truncates them on 64-bit.
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.GetForegroundWindow.argtypes = []
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.SetActiveWindow.restype = ctypes.c_void_p
user32.SetActiveWindow.argtypes = [ctypes.c_void_p]
user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.IsIconic.argtypes = [ctypes.c_void_p]
user32.SystemParametersInfoW.argtypes = [
    ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
# AttachThreadInput-based focus (see _focus): join our input queue to the foreground one.
user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]
kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
kernel32.GetCurrentThreadId.argtypes = []

# Process handles are pointers too, for the same reason.
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.QueryFullProcessImageNameW.argtypes = [
    ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
# GetApplicationUserModelId lives in kernel32 on Win8+, but declare it defensively: it is
# absent on older builds, and _aumid_for_pid treats that as "not a packaged app".
try:
    kernel32.GetApplicationUserModelId.restype = ctypes.c_long
    kernel32.GetApplicationUserModelId.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_wchar_p]
except AttributeError:
    pass

# Store apps (Calculator, Settings, ...) run inside this host process; their own process
# owns a child window of the host's frame. See _real_pid_for_hwnd.
APP_FRAME_HOST = "applicationframehost.exe"

# Shell surfaces that briefly take focus while you click around. Never a macro target.
# APP_FRAME_HOST is here only as a fallback: if we can't resolve the hosted app we must not
# record the host itself, which cannot be launched.
IGNORED_EXES = {
    "explorer.exe", "searchhost.exe", "searchapp.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "textinputhost.exe", "lockapp.exe", APP_FRAME_HOST,
}


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
user32.WindowFromPoint.restype = ctypes.c_void_p
user32.WindowFromPoint.argtypes = [_POINT]
user32.GetAncestor.restype = ctypes.c_void_p
user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
GA_ROOT = 2


def _pid_for_hwnd(hwnd):
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return pid.value


def _window_rect(hwnd):
    """(left, top, right, bottom) of a window in screen coordinates, or None."""
    if not hwnd:
        return None
    r = _RECT()
    if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r)):
        return None
    return r.left, r.top, r.right, r.bottom


def _window_origin(hwnd):
    """Top-left of a window in screen coordinates, or None.

    Clicks are stored relative to this so a macro still lands on the right control when
    the app reopens somewhere else on screen."""
    rect = _window_rect(hwnd)
    return (rect[0], rect[1]) if rect else None


def _is_own_window_at(x, y):
    """True if the window under (x, y) belongs to us - those clicks are UI, not macro."""
    try:
        hwnd = user32.WindowFromPoint(_POINT(int(x), int(y)))
        if not hwnd:
            return False
        root = user32.GetAncestor(ctypes.c_void_p(hwnd), GA_ROOT) or hwnd
        return _pid_for_hwnd(root) == os.getpid()
    except Exception:
        return False


def _exe_path_for_pid(pid):
    """Full path of a process's executable, or '' if it's gone / not ours to query."""
    if not pid:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_ulong(32768)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


def _exe_name_for_pid(pid):
    return os.path.basename(_exe_path_for_pid(pid)).lower()


def _normalise_exe(name):
    name = os.path.basename(name).lower()
    return name if name.endswith(".exe") else name + ".exe"


def _real_pid_for_hwnd(hwnd):
    """The process a window really belongs to.

    Store (UWP) apps don't own their own top-level window: ApplicationFrameHost.exe hosts
    the frame and the app's process owns a child CoreWindow. Recording the host would give
    us a target that cannot be launched, so resolve through to the hosted process."""
    pid = _pid_for_hwnd(hwnd)
    if _exe_name_for_pid(pid) != APP_FRAME_HOST:
        return pid
    hosted = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(child, _):
        cpid = _pid_for_hwnd(child)
        if cpid and cpid != pid:
            hosted.append(cpid)
            return False
        return True

    user32.EnumChildWindows(ctypes.c_void_p(hwnd), cb, 0)
    return hosted[0] if hosted else pid


def _aumid_for_pid(pid):
    """A packaged app's Application User Model ID, or '' for an ordinary executable.

    Store apps can't be started from their exe path (it's blocked); the AUMID is what makes
    them launchable, via `explorer shell:AppsFolder\\<aumid>`."""
    if not pid:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        length = ctypes.c_uint32(0)
        kernel32.GetApplicationUserModelId(h, ctypes.byref(length), None)   # size probe
        if not length.value:
            return ""
        buf = ctypes.create_unicode_buffer(length.value)
        if kernel32.GetApplicationUserModelId(h, ctypes.byref(length), buf) == 0:
            return buf.value
        return ""
    except Exception:
        return ""
    finally:
        kernel32.CloseHandle(h)


def app_target_for_pid(pid):
    """(launch command, exe basename) for a running process — what a macro needs to find
    the app again later and start it if it's gone."""
    path = _exe_path_for_pid(pid)
    if not path:
        return "", ""
    exe = os.path.basename(path).lower()
    aumid = _aumid_for_pid(pid)
    if aumid:
        return "explorer.exe shell:AppsFolder\\" + aumid, exe
    return path, exe


def foreground_app():
    """(pid, full exe path) of the foreground window, resolved through any UWP host.
    (0, '') for our own windows, shell surfaces, and anything we can't identify."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return 0, ""
    pid = _real_pid_for_hwnd(hwnd)
    if not pid or pid == os.getpid():
        return 0, ""
    path = _exe_path_for_pid(pid)
    if not path or os.path.basename(path).lower() in IGNORED_EXES:
        return 0, ""
    return pid, path


def _find_window(match):
    """First visible, titled top-level window whose (pid, exe basename) satisfies `match`.

    The pid tested is the *hosted* one, so Store apps match on their own executable, while
    the hwnd returned is still the top-level frame — the thing you actually focus."""
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            pid = _real_pid_for_hwnd(hwnd)
            if match(pid, _exe_name_for_pid(pid)):
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def _find_window_by_exe(exe):
    exe = _normalise_exe(exe)
    return _find_window(lambda pid, name: name == exe)


def _find_window_by_pid(pid):
    return _find_window(lambda p, name: p == pid) if pid else None


def _split_command(command):
    """'"C:/Program Files/App/app.exe" --flag' -> ('C:/Program Files/App/app.exe', '--flag').

    Handles quoted targets and unquoted paths containing spaces before falling back to
    splitting on whitespace, so auto-detected full paths survive a round trip."""
    command = (command or "").strip()
    if not command:
        return "", ""
    if command.startswith('"'):
        end = command.find('"', 1)
        if end > 0:
            return command[1:end], command[end + 1:].strip()
    if os.path.exists(command):
        return command, ""
    parts = command.split(None, 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _allow_foreground():
    """Drop the foreground-lock timeout to zero so SetForegroundWindow is not refused.

    Windows only lets a process raise a window if it owns the most recent input event.
    Macro playback runs on a background hotkey thread that does not, so the call is
    silently ignored - the window never comes forward. Setting the lock timeout to 0 lifts
    that restriction for our own SetForegroundWindow calls."""
    try:
        user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, ctypes.c_void_p(0), 0)
    except Exception:
        pass


def window_is_foreground(hwnd):
    """True if `hwnd`'s process owns the current foreground window.

    Compared by resolved pid, not handle, so a UWP app counts as foreground whether the
    OS reports its frame or a child window."""
    fg = user32.GetForegroundWindow()
    if not fg or not hwnd:
        return False
    return _real_pid_for_hwnd(fg) == _real_pid_for_hwnd(hwnd)


def _focus(hwnd, attempts=4):
    """Bring a window to the foreground and confirm it actually got there.

    Returns True only once the target owns the foreground - the caller relies on that to
    refuse to type into the wrong window. Synthesizes no keys (the old ALT tap left ALT
    stuck); instead it lifts the foreground lock, attaches to the current foreground
    thread's input queue, and as a last resort minimises+restores, which forces a window
    to the top when nothing else will."""
    _allow_foreground()
    hp = ctypes.c_void_p(hwnd)
    our_tid = kernel32.GetCurrentThreadId()
    for i in range(attempts):
        fg = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(ctypes.c_void_p(fg), None) if fg else 0
        attached = bool(fg_tid and fg_tid != our_tid
                        and user32.AttachThreadInput(our_tid, fg_tid, True))
        try:
            if user32.IsIconic(hp):
                user32.ShowWindow(hp, SW_RESTORE)
            user32.BringWindowToTop(hp)
            user32.SetForegroundWindow(hp)
            user32.SetActiveWindow(hp)
        finally:
            if attached:
                user32.AttachThreadInput(our_tid, fg_tid, False)
        if window_is_foreground(hwnd):
            return True
        # escalate: a minimise/restore cycle reliably yanks a stubborn window forward
        if i < attempts - 1:
            user32.ShowWindow(hp, SW_MINIMIZE)
            user32.ShowWindow(hp, SW_RESTORE)
            time.sleep(0.12)
            if window_is_foreground(hwnd):
                return True
    return window_is_foreground(hwnd)


def release_modifiers():
    """Force every modifier up. A stuck virtual modifier (usually ALT from a swallowed
    key-up) makes the keyboard type accelerators instead of characters; clearing it before
    recording and around playback keeps both the macro and the user able to type."""
    for mod in ("alt", "left alt", "right alt", "ctrl", "left ctrl", "right ctrl",
                "shift", "left shift", "right shift", "windows", "left windows"):
        try:
            keyboard.release(mod)
        except Exception:
            pass


def _launch(command):
    target, args = _split_command(command)
    try:
        if os.path.exists(target) and not args:
            os.startfile(target)
        else:
            subprocess.Popen(command, shell=True)
        return True
    except Exception:
        try:
            subprocess.Popen(command, shell=True)
            return True
        except Exception:
            return False


def ensure_app(command, pid=0, exe=""):
    """Put the macro's application in the foreground, starting it if it isn't running.

    Tries, in order: the exact instance recorded with the macro (`pid`), any window of
    the same executable, then a cold start — waiting up to LAUNCH_TIMEOUT for its first
    window rather than guessing at a fixed delay. Returns True only once the app is
    verified to hold the foreground, so a caller can refuse to type into the wrong window.

    `exe` is the executable to match windows against; it is passed separately because a
    Store app's launch command (`explorer shell:AppsFolder\\...`) says nothing about the
    process that ends up running."""
    if not command:
        return True
    exe = _normalise_exe(exe) if exe else _normalise_exe(_split_command(command)[0])

    hwnd = None
    if pid and _exe_name_for_pid(pid) == exe:   # guards against a recycled pid
        hwnd = _find_window_by_pid(pid)         # matches on the hosted pid, so UWP works
    if hwnd is None:
        hwnd = _find_window_by_exe(exe)
    if hwnd:
        ok = _focus(hwnd)
        time.sleep(FOCUS_DELAY)
        return ok or window_is_foreground(hwnd)

    if not _launch(command):
        return False
    deadline = time.time() + LAUNCH_TIMEOUT
    while time.time() < deadline:
        time.sleep(LAUNCH_POLL)
        hwnd = _find_window_by_exe(exe)
        if hwnd:
            ok = _focus(hwnd)
            time.sleep(LAUNCH_DELAY)
            return ok or window_is_foreground(hwnd)
    return False


class ForegroundWatcher(threading.Thread):
    """Samples the foreground window while a macro is being recorded, so the macro can
    remember which application the user clicked into. Our own windows are ignored, so
    the Recording overlay never registers as the target."""

    def __init__(self, interval=FOREGROUND_POLL):
        threading.Thread.__init__(self, daemon=True)
        self.interval = interval
        self.transitions = []          # (timestamp, target dict), in the order seen
        self._stop = threading.Event()

    def run(self):
        last = None
        while not self._stop.is_set():
            pid, path = foreground_app()
            if path and path != last:
                # resolve now, while the process is certainly still alive
                command, exe = app_target_for_pid(pid)
                if command:
                    self.transitions.append(
                        (time.time(), {"app": command, "app_exe": exe, "app_pid": pid}))
                    last = path
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()

    def app_at(self, when=None):
        """The app that had focus at `when` — normally the time of the first recorded
        keystroke, so switching away afterwards doesn't steal the binding. Falls back to
        the first app seen, or an empty target if nothing was identified."""
        empty = {"app": "", "app_exe": "", "app_pid": 0}
        if not self.transitions:
            return empty
        chosen = self.transitions[0]
        if when:
            for entry in self.transitions:
                if entry[0] > when:
                    break
                chosen = entry
        return chosen[1]


# ---------------------------------------------------------------- macro engine
class MouseRecorder:
    """Captures mouse clicks and wheel ticks while a macro is recorded.

    Only button and wheel events are kept, never raw movement: a move stream would bloat
    the macro for no benefit, and because each button event stores its own position, a
    drag still replays correctly (move → press → move → release).

    Each click records where it landed relative to its window's top-left as well as in
    absolute screen coordinates, so playback can follow the app if it opens elsewhere."""

    def __init__(self):
        self.events = []
        self._hook = None

    def start(self):
        if mouse is None:
            return False
        self._hook = mouse.hook(self._on_event)
        return True

    def stop(self):
        if self._hook is not None:
            try:
                mouse.unhook(self._hook)
            except Exception:
                pass
            self._hook = None

    def _on_event(self, e):
        try:
            if isinstance(e, mouse.WheelEvent):
                self.events.append({"src": "m", "t": e.time, "e": "wheel", "d": e.delta})
                return
            if not isinstance(e, mouse.ButtonEvent):
                return                       # MoveEvent - deliberately ignored
            # Windows reports the second click within the double-click interval as a
            # DOUBLE (WM_*BUTTONDBLCLK) rather than a DOWN. Physically it is still one
            # press, so store it as 'down' - the sequence down,up,down,up already IS a
            # double-click when the gaps are preserved. Recording 'double' verbatim and
            # replaying it as mouse.double_click() is what doubled every fast click.
            et = "down" if e.event_type == "double" else e.event_type
            x, y = mouse.get_position()
            if _is_own_window_at(x, y):      # our own Stop button etc.
                return
            hwnd = user32.GetForegroundWindow()
            origin = _window_origin(hwnd)
            self.events.append({
                "src": "m", "t": e.time, "e": et, "b": e.button,
                "x": x, "y": y,
                "rel": [x - origin[0], y - origin[1]] if origin else None,
            })
        except Exception:
            pass                             # a dropped click must not kill recording


def serialize_events(key_events, mouse_events=()):
    """Merge both input streams onto one timeline, normalised to start at 0.

    Keyboard and mouse hooks stamp events from the same clock, so interleaving is just a
    sort - which is what makes a macro that mixes typing and clicking replay in order."""
    out = [{"src": "k", "t": e.time, "e": e.event_type, "s": e.scan_code, "n": e.name}
           for e in key_events]
    out.extend(dict(d) for d in mouse_events)
    out.sort(key=lambda d: d["t"] or 0)
    if out:
        t0 = out[0]["t"] or 0
        for d in out:
            d["t"] = (d["t"] or t0) - t0
    return out


CLICK_MARGIN = 8   # px of slack around the target window when validating a click


def play_events(events, origin=None, bounds=None):
    """Replay a merged macro, preserving the original gaps between events.

    Written out rather than delegating to keyboard.play() because that can only replay
    keystrokes; interleaving clicks needs one loop driving both. `origin` is the target
    window's current top-left: when a click was recorded relative to a window, it is
    re-anchored here, so the macro follows the app rather than clicking blind screen
    coordinates.

    `bounds` is that window's current rectangle (left, top, right, bottom). When given,
    any click that resolves to a point outside it is dropped: a recording often opens with
    the click that summoned the app (on the taskbar or another window), whose coordinates
    no longer point at anything useful - replaying it lands on some unrelated window and,
    worse, steals the foreground away from the app we just focused. ensure_app has already
    put the target app in front, so a click that can't be placed on it is not ours to make."""
    pressed = set()

    def on_target(x, y):
        if bounds is None:
            return True
        l, t, r, b = bounds
        return (l - CLICK_MARGIN <= x <= r + CLICK_MARGIN
                and t - CLICK_MARGIN <= y <= b + CLICK_MARGIN)

    last = None
    for d in events:
        t = d.get("t") or 0
        if last is not None and t > last:
            time.sleep(min(t - last, MAX_EVENT_GAP))
        last = t

        if d.get("src", "k") == "k":
            key = d.get("s") or d.get("n")   # mirrors keyboard.play's own preference
            if key is None:
                continue
            (keyboard.press if d["e"] == "down" else keyboard.release)(key)
            continue

        if mouse is None:
            continue
        if d["e"] == "wheel":
            mouse.wheel(d.get("d", 0))
            continue
        rel = d.get("rel")
        if rel and origin:
            x, y = origin[0] + rel[0], origin[1] + rel[1]
        else:
            x, y = d.get("x"), d.get("y")
        if x is None or y is None or not on_target(x, y):
            continue
        mouse.move(x, y)
        # 'double' is a press too (its release is the following 'up'). New recordings never
        # store it - see MouseRecorder - but older ones might, so keep it a single press.
        btn = d.get("b", "left")
        if d["e"] in ("down", "double"):
            mouse.press(btn)
            pressed.add(btn)
        elif d["e"] == "up":
            mouse.release(btn)
            pressed.discard(btn)

    for btn in pressed:   # a down whose up was dropped must not leave the button held
        try:
            mouse.release(btn)
        except Exception:
            pass


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self._registered = {}   # keynum -> hotkey handle
        self.status_cb = None   # set by the GUI; called from playback threads

    def _status(self, msg):
        if self.status_cb:
            try:
                self.status_cb(msg)
            except Exception:
                pass

    def register_all(self):
        """Arm a listener for every key that both *sends* something and has a macro linked.

        Layer 1 is what the macropad key is programmed to emit (cfg["shortcuts"][keynum], e.g.
        "f13"); layer 2 is the macro linked to it (cfg["bindings"][keynum]). At runtime the key
        sends its keystroke and - if this app is running - we detect it and replay the macro."""
        self.unregister_all()
        shortcuts = self.cfg.get("shortcuts", {})
        for keynum, macro_name in self.cfg["bindings"].items():
            trigger = shortcuts.get(keynum)
            if trigger and macro_name in self.cfg["macros"]:
                self._register(keynum, trigger, macro_name)

    def _register(self, keynum, trigger, macro_name):
        # `trigger` is exactly what the key sends ("f13", "ctrl+alt+x", ...). A native multi-key
        # sequence ("ctrl+c ctrl+v") isn't a hotkey the OS can fire on, so add_hotkey rejects it
        # and that key just performs its keystrokes without triggering a macro.
        try:
            handle = keyboard.add_hotkey(trigger, self._run, args=(macro_name,),
                                         suppress=False, trigger_on_release=False)
        except Exception:
            return
        self._registered[keynum] = handle

    def unregister_all(self):
        for h in self._registered.values():
            try:
                keyboard.remove_hotkey(h)
            except Exception:
                pass
        self._registered.clear()

    def _run(self, macro_name):
        macro = self.cfg["macros"].get(macro_name)
        if not macro:
            return
        threading.Thread(target=self._play, args=(macro,), daemon=True).start()

    def _play(self, macro):
        app = macro.get("app", "")
        origin = None
        bounds = None
        if app:
            label = macro.get("app_exe") or os.path.basename(_split_command(app)[0])
            if not ensure_app(app, macro.get("app_pid", 0), macro.get("app_exe", "")):
                # Focus could not be confirmed; typing now would hit the wrong window.
                self._status("Could not focus %s - macro not run" % label)
                return
            # where the window sits *now*, so recorded clicks can be re-anchored to it
            # (and clicks that fall outside it, like the one that first summoned the app,
            # are dropped rather than replayed onto whatever is now at those coordinates)
            exe = macro.get("app_exe") or _split_command(app)[0]
            hwnd = _find_window_by_exe(exe)
            origin = _window_origin(hwnd)
            bounds = _window_rect(hwnd)
            # final guard: focus can slip between ensure_app and here
            if not window_is_foreground(hwnd):
                self._status("Lost focus on %s - macro not run" % label)
                return
        # clear the trigger key's modifiers so they don't contaminate the macro
        release_modifiers()
        try:
            play_events(macro["events"], origin, bounds)
        finally:
            # and clear anything the macro itself left held (a down with no matching up)
            release_modifiers()


# ---------------------------------------------------------------- device programming
def program_key_on_device(keynum):
    """Program the physical macropad key to send its exotic chord (via macropad.py)."""
    import macropad as m
    token = chord_macropad_token(keynum)
    mods = 0
    key = None
    for part in token.split("+"):
        if part in m.MODIFIERS:
            mods |= m.MODIFIERS[part]
        else:
            key = m.KEYCODES[part]
    with m.Macropad() as mp:
        mp.set_keyboard(int(keynum), [(mods, key)])


# ---------------------------------------------------------------- appearance
# Fluent-ish palettes. Tk can't do Mica or rounded corners, so "native" here means the
# right greys, the right accent, the system font and crisp DPI scaling — which is most of
# what makes the default Tk look dated.
PALETTES = {
    "light": dict(
        bg="#f3f3f3", surface="#ffffff", border="#e5e5e5", text="#1b1b1b",
        muted="#5d5d5d", accent="#0067c0", accent_hover="#0078d4", accent_text="#ffffff",
        btn="#fdfdfd", btn_hover="#f5f5f5", btn_press="#f0f0f0", btn_border="#d1d1d1",
        sel="#0067c0", sel_text="#ffffff",
        btn_off="#f0f0f0", disabled="#a3a3a3",
    ),
    "dark": dict(
        bg="#202020", surface="#2b2b2b", border="#383838", text="#ffffff",
        muted="#c5c5c5", accent="#4cc2ff", accent_hover="#63cbff", accent_text="#000000",
        btn="#2d2d2d", btn_hover="#363636", btn_press="#3d3d3d", btn_border="#414141",
        sel="#4cc2ff", sel_text="#000000",
        btn_off="#272727", disabled="#6f6f6f",
    ),
}


def _system_uses_light_theme():
    """Windows' own app theme preference, so we match the shell instead of fighting it."""
    try:
        import winreg
        path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            return bool(winreg.QueryValueEx(key, "AppsUseLightTheme")[0])
    except Exception:
        return True


def enable_dpi_awareness():
    """Opt in to per-monitor DPI before any window exists.

    Without this Windows bitmap-stretches the window on scaled displays, which is the
    single biggest reason a Tk app reads as blurry and old. Must run before Tk()."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)      # per-monitor v1
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def _dwm_set(hwnd, attr, value):
    """DwmSetWindowAttribute(hwnd, attr, &int(value)). Silently ignores unsupported
    attributes so the app still runs on older Windows builds."""
    v = ctypes.c_int(value)
    try:
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), ctypes.c_int(attr), ctypes.byref(v), ctypes.sizeof(v))
    except Exception:
        pass


def _use_dark_titlebar(root, dark):
    """Match the window frame to the Win11 Fluent signature experiences: a themed title
    bar (Color), rounded corners (Shapes and geometry), and - where the OS supports it - a
    Mica backdrop (Materials). Each is best-effort; unsupported ones are no-ops."""
    DWMWA_USE_IMMERSIVE_DARK_MODE = 20      # 19 on pre-20H1 builds
    DWMWA_WINDOW_CORNER_PREFERENCE = 33
    DWMWCP_ROUND = 2
    # Note: no Mica backdrop. It would need the client area to be translucent, but Tk paints
    # it opaque, so the material never shows - setting it would be dead code. Rounded corners
    # and the themed frame both take effect.
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        for attr in (DWMWA_USE_IMMERSIVE_DARK_MODE, 19):
            _dwm_set(hwnd, attr, 1 if dark else 0)
        _dwm_set(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
    except Exception:
        pass


def _round_borderless(win):
    """Round a borderless Toplevel's corners the Win11 way (DWM corner preference), for the
    custom context menu. Best-effort: a no-op on older Windows / other platforms."""
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetAncestor(win.winfo_id(), 2)  # GA_ROOT
        _dwm_set(hwnd, 33, 2)                                       # CORNER_PREFERENCE = ROUND
    except Exception:
        pass


def _pick_font(tkfont, candidates, size, weight="normal"):
    families = set(tkfont.families())
    for name in candidates:
        if name in families:
            return (name, size, weight)
    return ("Segoe UI", size, weight)


def system_accent(dark):
    """The user's chosen Windows accent as (fill, hover, text-on-accent), or None.

    Fluent's 'Personal' principle: an app should wear the accent the user picked, not a
    hardcoded blue. Windows stores 8 shades (three light, the base, three dark); it fills
    with a darker shade on light backgrounds and a lighter shade on dark ones so the accent
    stays legible either way. Returns None if the palette can't be read, so callers fall
    back to their built-in default."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent") as k:
            blob, _ = winreg.QueryValueEx(k, "AccentPalette")
    except Exception:
        return None
    if not blob or len(blob) < 28:
        return None

    def shade(i):
        return "#%02x%02x%02x" % (blob[i * 4], blob[i * 4 + 1], blob[i * 4 + 2])

    # palette index 0..2 = light shades, 3 = base, 4..6 = dark shades
    if dark:
        return shade(1), shade(0), "#000000"   # light fill, lighter hover, dark text
    return shade(4), shade(5), "#ffffff"        # dark fill, darker hover, white text


def _rounded_photo(size, radius, fill, outline, ow):
    """A rounded-rect RGBA PhotoImage, supersampled then downscaled for smooth corners.
    Transparent outside the radius so the button's corners show the parent background.

    Colour and alpha are built separately: a *solid* RGB image (fill everywhere, with the
    outline drawn on top) carries the colour, and a one-channel mask carries the rounded
    silhouette. Compositing fill over a transparent-black canvas and resampling RGBA
    together makes LANCZOS ring at the fill->transparent edge - the RGB channels overshoot
    toward white on a light fill, which showed up as white dots in the corners of the
    borderless (disabled) buttons. Keeping the RGB solid means only alpha fades at the rim,
    so the corners stay the fill colour and never flare white."""
    from PIL import Image, ImageDraw, ImageTk
    ss = 4
    w = size * ss
    inset = (ow * ss / 2.0) if outline else 0
    box = [inset, inset, w - 1 - inset, w - 1 - inset]
    rad = radius * ss

    rgb = Image.new("RGB", (w, w), fill)
    if outline and ow:
        ImageDraw.Draw(rgb).rounded_rectangle(box, radius=rad, fill=fill,
                                              outline=outline, width=max(1, ow) * ss)

    mask = Image.new("L", (w, w), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=rad, fill=255)

    rgb = rgb.resize((size, size), Image.LANCZOS)
    rgb.putalpha(mask.resize((size, size), Image.LANCZOS))
    return ImageTk.PhotoImage(rgb)


def _install_rounded_buttons(root, style, p, font):
    """Give ttk buttons Windows 11's 4px rounded corners (Fluent 'Shapes and geometry').

    clam only draws flat rectangles, so the corner radius comes from a themed 'image'
    element: a rounded-rect sliced with a fixed-size border so ttk stretches the flat
    middle and leaves the corners untouched at any button width. One image per visual
    state, rendered at the current DPI so 4px stays 4px and the corners stay crisp. If
    Pillow is missing the flat fallback style remains."""
    try:
        from PIL import Image  # noqa: F401 - presence check
    except Exception:
        return
    r = max(4, round(4 * root.winfo_fpixels("1i") / 96.0))   # 4 logical px at this DPI
    size = 2 * r + 3                                          # 3px stretchable centre
    imgs = root._rbtn_imgs = {}                               # keep refs alive past return

    def elem(name, states):
        made = {st: _rounded_photo(size, r, *spec) for st, spec in states.items()}
        imgs[name] = made
        ordered = [made[""]] + [(st, made[st]) for st in ("disabled", "pressed", "active")
                                if st in made]
        style.element_create(name, "image", *ordered, border=r, sticky="nsew", padding=0)

    elem("Rounded.button", {
        "": (p["btn"], p["btn_border"], 1),
        "active": (p["btn_hover"], p["btn_border"], 1),
        "pressed": (p["btn_press"], p["btn_border"], 1),
        # No outline when disabled: a contrasting rim on a fill that barely differs from
        # the window background reads as a floating border, not a greyed-out button.
        "disabled": (p["btn_off"], None, 0),
    })
    elem("RoundedAccent.button", {
        "": (p["accent"], None, 0),
        "active": (p["accent_hover"], None, 0),
        "pressed": (p["accent"], None, 0),
        "disabled": (p["btn_off"], None, 0),
    })
    for style_name, elem_name in (("TButton", "Rounded.button"),
                                  ("Accent.TButton", "RoundedAccent.button")):
        style.layout(style_name, [(elem_name, {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [
                ("Button.label", {"sticky": "nsew"})]})]})])


def apply_theme(root):
    """Style ttk from scratch on the 'clam' base and return (palette, fonts).

    'clam' rather than the native 'vista' theme: vista widgets ignore any attempt to
    recolour them, so they'd stay light while the rest of the window went dark. Styling a
    neutral theme by hand is what makes one coherent look possible in both modes."""
    import tkinter.font as tkfont
    from tkinter import ttk

    dark = not _system_uses_light_theme()
    p = dict(PALETTES["dark" if dark else "light"])   # copy: we override accent below
    accent = system_accent(dark)
    if accent:
        p["accent"], p["accent_hover"], p["accent_text"] = accent
        p["sel"], p["sel_text"] = accent[0], accent[2]   # selection wears the accent too
    body = _pick_font(tkfont, ("Segoe UI Variable Text", "Segoe UI"), 10)
    strong = _pick_font(tkfont, ("Segoe UI Variable Display", "Segoe UI"), 15, "bold")
    caption = _pick_font(tkfont, ("Segoe UI Variable Small", "Segoe UI"), 9)

    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(bg=p["bg"])

    style.configure(".", background=p["bg"], foreground=p["text"], font=body,
                    borderwidth=0, focuscolor=p["accent"])
    style.configure("TFrame", background=p["bg"])
    style.configure("Card.TFrame", background=p["surface"])
    style.configure("Border.TFrame", background=p["border"])
    style.configure("TLabel", background=p["bg"], foreground=p["text"], font=body)
    style.configure("Title.TLabel", font=strong)
    style.configure("Muted.TLabel", foreground=p["muted"], font=caption)
    style.configure("Status.TLabel", background=p["surface"], foreground=p["muted"],
                    font=caption, padding=(12, 7))

    # The rounded corners come from image elements installed below; here we set only what
    # the button layout still reads - text colour, font, and the label's inset padding.
    #
    # `background` must be the colour of whatever the button sits on (the buttons live on
    # plain TFrame bars, i.e. the window bg). The rounded image has transparent corners, so
    # the field background shows through them; clam's default is a light grey, which flared
    # as white brackets in the corners of the borderless (disabled) buttons. Every state is
    # pinned, or clam's per-state map would put the light default back for disabled/active.
    btn_bg = [("disabled", p["bg"]), ("active", p["bg"]), ("pressed", p["bg"])]
    style.configure("TButton", foreground=p["text"], background=p["bg"], font=body,
                    padding=(14, 7), anchor="center", borderwidth=0)
    style.map("TButton", foreground=[("disabled", p["disabled"])], background=btn_bg)
    style.configure("Accent.TButton", foreground=p["accent_text"], background=p["bg"],
                    font=body, padding=(14, 7), anchor="center", borderwidth=0)
    style.map("Accent.TButton", background=btn_bg,
              foreground=[("disabled", p["disabled"]),
                          ("pressed", p["accent_text"]), ("active", p["accent_text"])])
    _install_rounded_buttons(root, style, p, body)

    # Win11 scrollbars are a thin thumb with no arrow buttons; drop them from the layout.
    style.layout("Thin.Vertical.TScrollbar", [
        ("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    style.configure("Thin.Vertical.TScrollbar", width=8, background=p["border"],
                    troughcolor=p["surface"], bordercolor=p["surface"],
                    lightcolor=p["border"], darkcolor=p["border"])
    style.map("Thin.Vertical.TScrollbar", background=[("active", p["muted"])])

    style.configure("Treeview", background=p["surface"], fieldbackground=p["surface"],
                    foreground=p["text"], font=body, rowheight=30, borderwidth=0)
    style.map("Treeview", background=[("selected", p["sel"])],
              foreground=[("selected", p["sel_text"])])
    style.configure("Treeview.Heading", background=p["surface"], foreground=p["muted"],
                    font=caption, relief="flat", padding=(10, 8), borderwidth=0)
    style.map("Treeview.Heading", background=[("active", p["btn_hover"])])
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])   # no inner border

    # Dark text field for the themed input dialogs. clam's Entry is otherwise light grey.
    style.configure("TEntry", fieldbackground=p["surface"], foreground=p["text"],
                    insertcolor=p["text"], bordercolor=p["border"], lightcolor=p["border"],
                    darkcolor=p["border"], borderwidth=1, padding=6)
    style.map("TEntry", bordercolor=[("focus", p["accent"])])

    _use_dark_titlebar(root, dark)
    return p, {"body": body, "strong": strong, "caption": caption}


def make_table(parent, columns, widths):
    """A two-column list view inside a hairline card. Returns (card frame, treeview).

    Treeview rather than Listbox for two reasons: real columns line up (space-padding a
    proportional font does not), and rows carry an `iid`, so selection maps straight back
    to a macro name or key number instead of a positional index."""
    from tkinter import ttk

    border = ttk.Frame(parent, style="Border.TFrame", padding=1)
    card = ttk.Frame(border, style="Card.TFrame")
    card.pack(fill="both", expand=True)

    keys = tuple(str(i) for i in range(len(columns)))
    tree = ttk.Treeview(card, columns=keys, show="headings", selectmode="browse")
    for k, title, width in zip(keys, columns, widths):
        tree.heading(k, text=title, anchor="w")
        tree.column(k, width=width, anchor="w", stretch=(k == keys[-1]))
    bar = ttk.Scrollbar(card, orient="vertical", style="Thin.Vertical.TScrollbar",
                        command=tree.yview)
    tree.pack(side="left", fill="both", expand=True)

    def autohide(first, last):
        """Show the scrollbar only when there is something to scroll, as Win11 does -
        otherwise a full-height thumb sits there looking like a stray divider."""
        if float(first) <= 0.0 and float(last) >= 1.0:
            bar.pack_forget()
        else:
            bar.pack(side="right", fill="y", pady=2, before=tree)   # keep a stable order
        bar.set(first, last)

    tree.configure(yscrollcommand=autohide)
    return border, tree


# ---------------------------------------------------------------- GUI
def run_gui(cfg, engine):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("Macro Studio")
    p, fonts = apply_theme(root)
    root.geometry("960x540")
    # minsize is set from the built layout further down (see set_min_size), so the window can
    # never be dragged smaller than the point where its buttons stay fully visible.

    recording = {"events": None, "hook": None, "watcher": None, "mouse": None}

    # --- layout -------------------------------------------------------------
    # Pack order is load-bearing. Each pack() carves a slab off the remaining cavity, so
    # the full-width status bar must be claimed BEFORE the side-by-side panes; otherwise
    # the panes take the whole height and the status bar lands in the leftover strip down
    # the right-hand side.
    # header: title/subtitle on the left, the one global action (Record new) on the right.
    # The button itself is created later, once record_new is defined, and packed into here.
    header = ttk.Frame(root, padding=(20, 16, 20, 12))
    header.pack(side="top", fill="x")
    htext = ttk.Frame(header)
    htext.pack(side="left", fill="x", expand=True)
    ttk.Label(htext, text="Macro Studio", style="Title.TLabel").pack(anchor="w")
    ttk.Label(htext, text="Record a macro, then bind it to a key on the macropad.",
              style="Muted.TLabel").pack(anchor="w", pady=(2, 0))

    btns = {}   # action-name -> ttk.Button, so selection can enable/disable them

    status_wrap = ttk.Frame(root, style="Border.TFrame", padding=(0, 1, 0, 0))
    status_wrap.pack(side="bottom", fill="x")          # before the panes - see above
    status = ttk.Label(status_wrap, text="Ready", style="Status.TLabel", anchor="w")
    status.pack(fill="x")

    panes = ttk.Frame(root, padding=(20, 0, 20, 16))
    panes.pack(side="top", fill="both", expand=True)
    panes.columnconfigure(0, weight=1, uniform="pane")
    panes.columnconfigure(1, weight=1, uniform="pane")
    panes.rowconfigure(1, weight=1)

    ttk.Label(panes, text="Macros").grid(row=0, column=0, sticky="w", pady=(0, 6))
    ttk.Label(panes, text="Macropad keys").grid(
        row=0, column=1, sticky="w", padx=(16, 0), pady=(0, 6))

    macro_card, macro_list = make_table(panes, ("Name", "Application"), (170, 150))
    macro_card.grid(row=1, column=0, sticky="nsew")
    key_card, key_list = make_table(panes, ("Key", "Sends", "Runs macro"), (120, 100, 140))
    key_card.grid(row=1, column=1, sticky="nsew", padx=(16, 0))

    # Bottom-left bar holds the global actions (Record new / Export / Import); per-macro
    # actions live on a right-click context menu. Key-pane actions sit under the key list.
    macro_bar = ttk.Frame(panes)
    macro_bar.grid(row=2, column=0, sticky="w", pady=(10, 0))
    key_bar = ttk.Frame(panes)
    key_bar.grid(row=2, column=1, sticky="w", padx=(16, 0), pady=(10, 0))

    def set_status(msg):
        status.config(text=msg)

    def ask_string(title, prompt, initial=""):
        """Themed replacement for simpledialog.askstring: dark, rounded, ttk widgets, with an
        accent OK. Returns the entered string, or None if cancelled."""
        win = tk.Toplevel(root)
        win.title(title)
        win.transient(root)
        win.resizable(False, False)
        win.configure(bg=p["bg"])
        _use_dark_titlebar(win, not _system_uses_light_theme())
        frame = ttk.Frame(win, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=prompt, style="Muted.TLabel", justify="left").pack(anchor="w")
        var = tk.StringVar(value=initial)
        entry = ttk.Entry(frame, textvariable=var, width=40, font=fonts["body"])
        entry.pack(fill="x", pady=(10, 0))
        entry.focus_set()
        entry.selection_range(0, "end")

        out = {"v": None}

        def ok(_=None):
            out["v"] = var.get()
            win.destroy()

        row = ttk.Frame(frame)
        row.pack(anchor="e", pady=(18, 0))
        ttk.Button(row, text="OK", style="Accent.TButton", command=ok).pack(side="left",
                                                                            padx=(0, 8))
        ttk.Button(row, text="Cancel", command=win.destroy).pack(side="left")
        entry.bind("<Return>", ok)
        win.bind("<Escape>", lambda e: win.destroy())

        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        px = root.winfo_rootx() + (root.winfo_width() - w) // 2
        py = root.winfo_rooty() + max(40, (root.winfo_height() - h) // 3)
        win.geometry("%dx%d+%d+%d" % (w, h, px, py))
        win.grab_set()
        win.wait_window()
        return out["v"]

    # playback runs on worker threads; marshal their status back onto the tk thread
    engine.status_cb = lambda msg: root.after(0, lambda: set_status(msg))

    def refresh():
        """Redraw both panes, preserving whatever was selected. Rows use the macro name /
        key number as their iid, so selection survives the rebuild for free."""
        macro_sel = macro_list.selection()
        key_sel = key_list.selection()

        macro_list.delete(*macro_list.get_children())
        for name in sorted(cfg["macros"]):
            entry = cfg["macros"][name]
            app = entry.get("app", "")
            label = (entry.get("app_exe")
                     or (os.path.basename(_split_command(app)[0]) if app else "—"))
            macro_list.insert("", "end", iid=name, values=(name, label))

        key_list.delete(*key_list.get_children())
        for keynum in KEY_ORDER:
            # two independent layers: what the key sends (device) and the macro the app runs
            # when it detects that (app). Either, both, or neither may be set.
            sends = cfg["shortcuts"].get(keynum) or "—"
            runs = cfg["bindings"].get(keynum) or "—"
            key_list.insert("", "end", iid=keynum,
                            values=(KEY_LABELS[keynum], sends, runs))

        for tree, sel in ((macro_list, macro_sel), (key_list, key_sel)):
            keep = [i for i in sel if tree.exists(i)]
            if keep:
                tree.selection_set(keep)
        update_states()

    def selected_macro():
        sel = macro_list.selection()
        return sel[0] if sel else None

    def selected_keynum():
        sel = key_list.selection()
        return sel[0] if sel else None

    def update_states(*_):
        """Enable each action only when its target is selected - a Delete with no macro
        chosen, or a Bind with no key, is simply not clickable. Safe to call before all
        buttons exist (missing ones are skipped)."""
        sel_macro = selected_macro()
        has_macro = sel_macro is not None
        keynum = selected_keynum()
        # "used" = a chord-trigger binding OR a native shortcut; either can be cleared
        key_used = keynum is not None and (keynum in cfg["bindings"]
                                           or keynum in cfg["shortcuts"])

        def enable(name, on):
            b = btns.get(name)
            if b is not None:
                b.state(["!disabled"] if on else ["disabled"])

        enable("bind", has_macro and keynum is not None)
        enable("unbind", key_used)
        enable("assign_shortcut", keynum is not None)

    # --- recording ---
    def record_new():
        name = ask_string("Record macro", "Name for this macro:")
        if not name:
            return
        overlay = tk.Toplevel(root)
        overlay.title("Recording")
        overlay.attributes("-topmost", True)
        overlay.geometry("380x180")
        overlay.configure(bg=p["bg"])
        _use_dark_titlebar(overlay, not _system_uses_light_theme())
        body = ttk.Frame(overlay, padding=20)
        body.pack(fill="both", expand=True)
        head = "Recording keystrokes…" if mouse is None else "Recording keys and clicks…"
        ttk.Label(body, text=head, style="Title.TLabel").pack(anchor="w")
        tip = ("Click into the application you want this macro to run in,\n"
               "type it, then click Stop.")
        if mouse is None:
            tip += "\n(Mouse capture off - run 'pip install mouse' to record clicks.)"
        ttk.Label(body, text=tip, style="Muted.TLabel", justify="left").pack(
            anchor="w", pady=(6, 0))
        # Silence the macro triggers while recording: a macropad key pressed now would fire
        # a playback that presses keys into our recording and fights the hook. Restored in
        # stop(). Clear any modifier that is already stuck so recording starts clean.
        engine.unregister_all()
        release_modifiers()
        recording["events"] = []
        recording["hook"] = keyboard.hook(lambda e: recording["events"].append(e))
        recording["mouse"] = MouseRecorder()
        recording["mouse"].start()
        recording["watcher"] = ForegroundWatcher()
        recording["watcher"].start()

        def stop():
            if recording["hook"]:
                keyboard.unhook(recording["hook"])
                recording["hook"] = None
            mrec, recording["mouse"] = recording.get("mouse"), None
            if mrec:
                mrec.stop()
            watcher, recording["watcher"] = recording["watcher"], None
            raw = recording["events"] or []
            mouse_events = mrec.events if mrec else []
            if watcher:
                watcher.stop()
            # bind to whatever had focus at the first input (key OR click)
            first_t = min([e.time for e in raw] + [d["t"] for d in mouse_events],
                          default=None)
            target = (watcher.app_at(first_t) if watcher
                      else {"app": "", "app_exe": "", "app_pid": 0})
            events = serialize_events(raw, mouse_events)
            overlay.destroy()
            macro = {"events": events}
            macro.update(target)
            cfg["macros"][name] = macro
            save_config(cfg)
            release_modifiers()       # don't leave a held modifier behind after recording
            engine.register_all()     # re-arm the triggers silenced at record start
            refresh()
            where = (f"-> {target['app_exe']} (pid {target['app_pid']})" if target["app"]
                     else "- no app detected, use Set App...")
            note = "" if events else "  NOTE: nothing recorded - did you type or click?"
            set_status(f"Saved '{name}' ({len(events)} events) {where}{note}")

        ttk.Button(body, text="Stop recording", style="Accent.TButton",
                   command=stop).pack(anchor="w", pady=(16, 0))
        overlay.protocol("WM_DELETE_WINDOW", stop)

    def capture_combo(prompt):
        """Modal capture of a keyboard combination -> its (modifier, keycode) chords, or None.

        The presses are reduced to the (modifier, keycode) chords the firmware stores, so the
        preview is exactly what it will send. Triggers are silenced during capture so a
        macropad key can't fire a playback into it."""
        win = tk.Toplevel(root)
        win.title("Bind to key")
        win.attributes("-topmost", True)
        win.geometry("420x250")
        win.configure(bg=p["bg"])
        _use_dark_titlebar(win, not _system_uses_light_theme())
        body = ttk.Frame(win, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=prompt, style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="Press the keys, then Save. Up to 5 keystrokes; Clear to redo.\n"
                             "(The combo also reaches the app behind this window.)",
                  style="Muted.TLabel", justify="left").pack(anchor="w", pady=(6, 0))
        preview = ttk.Label(body, text="(press keys…)", style="Title.TLabel")
        preview.pack(anchor="w", pady=(12, 0))

        state = {"events": [], "strokes": None, "result": None}
        engine.unregister_all()
        release_modifiers()

        def recompute():
            state["strokes"] = macro_as_keystrokes(
                {"events": serialize_events(state["events"]), "app": ""})
            preview.config(text=keystrokes_text(state["strokes"]) if state["strokes"]
                           else "(press keys…)")
            save_btn.state(["!disabled"] if state["strokes"] else ["disabled"])

        def on_key(e):
            state["events"].append(e)
            root.after(0, recompute)

        hook = keyboard.hook(on_key)

        # F13..F24 have no physical key, so they can't be pressed - offer them in a menu that
        # matches the app's other right-click menu. They're the ideal macro trigger: the app
        # can detect them and nothing else ever sends them.
        trig_row = ttk.Frame(body)
        trig_row.pack(anchor="w", pady=(10, 0))
        ttk.Label(trig_row, text="…or a trigger key (for macros):",
                  style="Muted.TLabel").pack(side="left")
        trig_btn = ttk.Button(trig_row, text="Choose  ▾")
        trig_btn.pack(side="left", padx=(8, 0))

        def choose_trigger():
            import macropad as m

            def make(fn):
                def pick():
                    state["events"] = []               # a picked trigger replaces any presses
                    state["strokes"] = [(0, m.KEYCODES[fn])]
                    preview.config(text=fn)
                    save_btn.state(["!disabled"])
                    trig_btn.config(text=fn.upper() + "  ▾")
                return pick

            items = [("F%d" % n, make("f%d" % n)) for n in range(13, 25)]
            show_popup_menu(items, trig_btn.winfo_rootx(),
                            trig_btn.winfo_rooty() + trig_btn.winfo_height())

        trig_btn.config(command=choose_trigger)

        def finish(save):
            keyboard.unhook(hook)
            release_modifiers()
            engine.register_all()
            state["result"] = state["strokes"] if save else None
            win.destroy()

        def clear():
            state["events"] = []
            recompute()

        row = ttk.Frame(body)
        row.pack(anchor="w", pady=(16, 0))
        save_btn = ttk.Button(row, text="Save", style="Accent.TButton",
                              command=lambda: finish(True))
        save_btn.pack(side="left", padx=(0, 8))
        save_btn.state(["disabled"])
        ttk.Button(row, text="Clear", command=clear).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Cancel", command=lambda: finish(False)).pack(side="left")

        win.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        win.grab_set()
        win.wait_window()
        return state["result"]

    def assign_shortcut():
        """Bind to key (layer 1): program the selected key to *send* a chosen keystroke.

        Capture a real combo, or pick a trigger key (F13..F24 - no physical key sends them, so
        they're collision-proof macro triggers). Written to the pad's flash, so it sends this on
        any PC with no app. If a macro is linked to this key (layer 2), the app now listens for
        this new keystroke instead - the two coexist."""
        keynum = selected_keynum()
        if not keynum:
            set_status("Select a key first"); return
        strokes = capture_combo(f"What should {KEY_LABELS[keynum]} send?")
        if not strokes:
            return
        text = keystrokes_text(strokes)
        try:
            import macropad as m
            with m.Macropad() as mp:
                mp.set_keyboard(int(keynum), strokes)
        except Exception as e:
            messagebox.showerror("Device error",
                                 f"Could not program the macropad:\n{e}\n\nIs it plugged in?")
            return
        _replace_layout_line(keynum, {"type": "key", "keys": text, "note": f"Sends: {text}"})
        cfg["shortcuts"][keynum] = text       # layer 1; any linked macro (layer 2) stays
        save_config(cfg); engine.register_all(); refresh()   # re-arm: the sent key changed
        linked = cfg["bindings"].get(keynum)
        if linked:
            set_status(f"{KEY_LABELS[keynum]} now sends {text} -> the app runs '{linked}' on it")
        else:
            set_status(f"{KEY_LABELS[keynum]} now sends {text} (works on any PC, no app needed)")

    def rename_macro():
        old = selected_macro()
        if not old:
            set_status("Select a macro first"); return
        new = ask_string("Rename macro", "New name:", initial=old)
        if new is None:
            return
        new = new.strip()
        if not new or new == old:
            return
        if new in cfg["macros"]:
            set_status(f"A macro named '{new}' already exists"); return
        cfg["macros"][new] = cfg["macros"].pop(old)
        bound = [k for k, v in cfg["bindings"].items() if v == old]
        for k in bound:
            cfg["bindings"][k] = new
        save_config(cfg); engine.register_all()
        for k in bound:                       # keep the layout.json notes honest
            record_in_layout(k, new)
        refresh()
        macro_list.selection_set(new)
        set_status(f"Renamed '{old}' to '{new}'")

    def delete_macro():
        name = selected_macro()
        if not name:
            set_status("Select a macro first"); return
        bound = [k for k, v in cfg["bindings"].items() if v == name]
        extra = (f"\n\nIt is bound to {len(bound)} key(s); those bindings will be removed."
                 if bound else "")
        if not messagebox.askyesno("Delete", f"Delete macro '{name}'?{extra}"):
            return
        cfg["macros"].pop(name, None)
        for k in bound:
            cfg["bindings"].pop(k, None)
        save_config(cfg); engine.register_all(); refresh()
        # the freed keys still physically emit their triggers; offer to hand them back
        # their normal function so the device doesn't keep firing dead triggers.
        if bound and messagebox.askyesno(
                "Restore keys",
                f"Restore {len(bound)} freed key(s) to their default function on the device?"):
            msgs = [f"{KEY_LABELS[k]} {restore_key_default(k)[1]}" for k in bound]
            refresh()
            set_status(f"Deleted '{name}'; " + "; ".join(msgs))
        else:
            set_status(f"Deleted '{name}'")

    def set_app():
        name = selected_macro()
        if not name:
            set_status("Select a macro first"); return
        cur = cfg["macros"][name].get("app", "")
        app = ask_string(
            "Application context",
            "App to launch/focus before running (exe name, full path, or URL).\n"
            "Leave blank for none.\nExamples:  code   |   chrome   |   notepad.exe",
            initial=cur)
        if app is None:
            return
        cfg["macros"][name]["app"] = app.strip()
        cfg["macros"][name]["app_pid"] = 0   # a hand-picked app has no recorded instance
        cfg["macros"][name]["app_exe"] = ""  # derive the match from the command instead
        save_config(cfg); refresh()
        set_status(f"Set app context for '{name}'")

    def test_macro():
        name = selected_macro()
        if not name:
            set_status("Select a macro first"); return
        entry = cfg["macros"][name]
        app = entry.get("app", "")
        # app_exe first: a Store app's command is 'explorer.exe shell:...', which would
        # otherwise report the launcher rather than the app.
        where = (entry.get("app_exe") or os.path.basename(_split_command(app)[0])
                 if app else "the focused window")
        set_status(f"Running '{name}' in 2s -> {where}")
        # via _run: playback threads off the GUI, which may block while an app starts
        root.after(2000, lambda: engine._run(name))

    def bind():
        """Map Macro (layer 2): link a recorded macro to a key.

        The app plays it when it detects whatever the key *sends* (layer 1). If the key has no
        'sends' yet, give it its default trigger (F13..F21) and program that onto the device so
        the link works immediately; if it already sends something (set via Bind to key), we
        just listen for that instead - the two layers coexist."""
        name = selected_macro(); keynum = selected_keynum()
        if not name or not keynum:
            set_status("Select a macro AND a key"); return
        cfg["bindings"][keynum] = name
        trigger = cfg["shortcuts"].get(keynum)
        if not trigger:                       # give it the default trigger and program the pad
            trigger = chord_macropad_token(keynum)      # F13..F21 for this key
            try:
                program_key_on_device(keynum)
                cfg["shortcuts"][keynum] = trigger
                record_in_layout(keynum, name)
            except Exception as e:
                save_config(cfg); engine.register_all(); refresh()
                set_status(f"Linked '{name}' to {KEY_LABELS[keynum]}, but couldn't program the"
                           f" pad ({e}) - plug it in and run Map Macro again")
                return
        save_config(cfg); engine.register_all(); refresh()
        set_status(f"{KEY_LABELS[keynum]} sends {trigger} -> runs '{name}' while the app is open")

    def unbind():
        keynum = selected_keynum()
        if not keynum:
            return
        had_macro = cfg["bindings"].pop(keynum, None)
        had_short = cfg["shortcuts"].pop(keynum, None)
        save_config(cfg); engine.register_all(); refresh()
        if had_macro is None and had_short is None:
            set_status(f"{KEY_LABELS[keynum]} was not bound"); return
        # the key still physically sends its trigger/shortcut; offer its normal job back
        if messagebox.askyesno(
                "Restore key",
                f"Cleared {KEY_LABELS[keynum]}.\n\nRestore it to its default function "
                f"({default_key_desc(keynum)}) on the device?"):
            ok, msg = restore_key_default(keynum)
            refresh()
            set_status(f"Cleared {KEY_LABELS[keynum]} - {msg}")
        elif had_short is not None:
            set_status(f"Cleared {KEY_LABELS[keynum]} (device still sends {had_short} until"
                       f" reprogrammed)")
        else:
            set_status(f"Unbound {KEY_LABELS[keynum]}"
                       f" (key still sends {chord_macropad_token(keynum)})")

    def export_binds():
        if not cfg["macros"]:
            set_status("No macros to export yet"); return
        path = filedialog.asksaveasfilename(
            parent=root, title="Export binds", defaultextension=".json",
            initialfile="macro-studio-binds.json",
            filetypes=[("Macro Studio binds", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            export_bundle(path, cfg)
        except Exception as e:
            messagebox.showerror("Export failed", str(e)); return
        set_status(f"Exported {len(cfg['macros'])} macro(s) to {os.path.basename(path)}")

    def import_binds():
        path = filedialog.askopenfilename(
            parent=root, title="Import binds",
            filetypes=[("Macro Studio binds", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            macros, bindings, shortcuts = import_bundle(path)
        except Exception as e:
            messagebox.showerror("Import failed", f"Could not read that file:\n{e}"); return
        if not macros and not shortcuts:
            set_status("That file had no binds"); return
        clash = [n for n in macros if n in cfg["macros"]]
        if clash and not messagebox.askyesno(
                "Import binds",
                f"Import {len(macros)} macro(s)?\n\n{len(clash)} will overwrite macros of the "
                "same name here.\n\nThe device is not reprogrammed - run 'Map Macro' (chord "
                "binds) or 'Bind to key' (native combos) afterwards to arm the keys here."):
            return
        cfg["macros"].update(macros)
        cfg["bindings"].update(bindings)
        cfg["shortcuts"].update(shortcuts)   # the two layers coexist per key
        save_config(cfg); engine.register_all(); refresh()
        set_status(f"Imported {len(macros)} macro(s), {len(shortcuts)} shortcut(s) - "
                   "arm the device with 'Map Macro' / 'Bind to key'")

    def add_button(parent, name, txt, fn, accent=False):
        b = ttk.Button(parent, text=txt, command=fn,
                       style="Accent.TButton" if accent else "TButton")
        b.pack(side="left", padx=(0, 8))
        btns[name] = b

    # Global actions in the bottom-left bar, under the macro list. Record new is the single
    # accent button (Fluent's one-primary rule); Export/Import move the whole bind set.
    btns["record"] = ttk.Button(macro_bar, text="Record new", style="Accent.TButton",
                                command=record_new)
    btns["record"].pack(side="left", padx=(0, 8))
    btns["export"] = ttk.Button(macro_bar, text="Export…", command=export_binds)
    btns["export"].pack(side="left", padx=(0, 8))
    btns["import"] = ttk.Button(macro_bar, text="Import…", command=import_binds)
    btns["import"].pack(side="left")

    # macro-pane actions, then key-pane actions - each under the list it operates on
    add_button(key_bar, "bind", "Map Macro", bind)
    add_button(key_bar, "assign_shortcut", "Bind to key", assign_shortcut)
    add_button(key_bar, "unbind", "Unbind", unbind)

    # macro actions live on a right-click menu instead of a button bar. tk.Menu is the dated
    # Win95-style control, so this is a hand-built popup: a rounded, padded, dark Toplevel with
    # Fluent hover highlights, matching the rest of the window.
    def show_popup_menu(items, x, y):
        """items = [(label, command) | None(separator)]; posts a Win11-style menu at x,y."""
        s = max(1.0, root.winfo_fpixels("1i") / 96.0)   # DPI scale for paddings
        prev_grab = root.grab_current()   # e.g. a modal dialog we're opening on top of
        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=p["border"])                    # 1px hairline border around the card
        card = tk.Frame(win, bg=p["surface"])
        card.pack(padx=1, pady=1)
        body = tk.Frame(card, bg=p["surface"])
        body.pack(padx=int(4 * s), pady=int(4 * s))

        closed = {"v": False}

        def close(_=None):
            if closed["v"]:
                return
            closed["v"] = True
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()
            if prev_grab is not None:     # hand modality back to the dialog underneath
                try:
                    prev_grab.grab_set()
                except Exception:
                    pass

        def run(cmd):
            close()
            root.after(1, cmd)          # let the menu tear down before the action's own dialog

        for it in items:
            if it is None:
                tk.Frame(body, bg=p["border"], height=1).pack(
                    fill="x", padx=int(8 * s), pady=int(4 * s))
                continue
            label, cmd = it
            row = tk.Label(body, text=label, bg=p["surface"], fg=p["text"], anchor="w",
                           font=fonts["body"], padx=int(14 * s), pady=int(7 * s))
            row.pack(fill="x")
            row.bind("<Enter>", lambda e, w=row: w.configure(bg=p["btn_hover"]))
            row.bind("<Leave>", lambda e, w=row: w.configure(bg=p["surface"]))
            row.bind("<Button-1>", lambda e, c=cmd: run(c))

        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry("%dx%d+%d+%d" % (w, h, max(0, min(x, sw - w - 4)),
                                      max(0, min(y, sh - h - 4))))
        _round_borderless(win)

        def on_outside(e):
            # the local grab funnels every click to this window, so dismiss when a press lands
            # outside its on-screen bounds (screen coords, robust to which child got the event)
            wx, wy, ww, wh = (win.winfo_rootx(), win.winfo_rooty(),
                              win.winfo_width(), win.winfo_height())
            if not (wx <= e.x_root < wx + ww and wy <= e.y_root < wy + wh):
                close()

        win.bind("<Button-1>", on_outside)
        win.bind("<Button-3>", on_outside)
        win.bind("<Escape>", close)
        win.bind("<FocusOut>", close)   # alt-tab / other-app click -> dismiss
        win.focus_force()
        win.grab_set()

    def macro_context(event):
        row = macro_list.identify_row(event.y)   # select the row under the cursor first, so
        if not row:                              # the action targets what was right-clicked
            return
        macro_list.selection_set(row)
        update_states()
        show_popup_menu([("Rename", rename_macro), ("Set app…", set_app), ("Test", test_macro),
                         None, ("Delete", delete_macro)], event.x_root, event.y_root)

    macro_list.bind("<Button-3>", macro_context)

    # keep buttons in step with the selection, and offer the native direct interactions
    macro_list.bind("<<TreeviewSelect>>", update_states)
    key_list.bind("<<TreeviewSelect>>", update_states)
    macro_list.bind("<Double-1>", lambda e: test_macro())
    macro_list.bind("<Return>", lambda e: test_macro())
    macro_list.bind("<Delete>", lambda e: delete_macro())
    key_list.bind("<Double-1>", lambda e: bind())
    key_list.bind("<Return>", lambda e: bind())
    key_list.bind("<Delete>", lambda e: unbind())

    refresh()

    # Clamp the minimum window size to what the fully-built layout actually needs, so no edge
    # drag can hide a button. reqwidth/reqheight reflect every packed child at this point; a
    # small margin absorbs title-bar/scrollbar rounding.
    def set_min_size():
        root.update_idletasks()
        root.minsize(root.winfo_reqwidth() + 8, root.winfo_reqheight() + 8)
    set_min_size()

    # --- tray ---
    def start_tray():
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            return
        img = Image.new("RGB", (64, 64), (24, 24, 28))
        d = ImageDraw.Draw(img)
        d.rectangle([10, 10, 54, 54], outline=(120, 220, 160), width=4)
        d.ellipse([26, 26, 38, 38], fill=(120, 220, 160))

        def show(icon=None, item=None):
            root.after(0, lambda: (root.deiconify(), root.lift()))

        def quit_all(icon=None, item=None):
            icon.stop()
            root.after(0, root.destroy)

        def open_config_folder(icon=None, item=None):
            # macros.json lives beside the .exe (portable) - open its folder so the config
            # is easy to copy onto another machine or a USB stick.
            try:
                os.startfile(os.path.dirname(CONFIG_PATH))
            except Exception:
                pass

        menu = pystray.Menu(
            pystray.MenuItem("Open Macro Studio", show, default=True),
            pystray.MenuItem("Open config folder", open_config_folder),
            pystray.MenuItem("Exit", quit_all),
        )
        icon = pystray.Icon("MacroStudio", img, "Macro Studio v%s" % __version__, menu)
        threading.Thread(target=icon.run, daemon=True).start()
        return icon

    def hide_to_tray():
        root.withdraw()
        set_status("Minimised to tray")

    root.protocol("WM_DELETE_WINDOW", hide_to_tray)
    start_tray()
    root.mainloop()


# ---------------------------------------------------------------- entry
def _seed_data_files():
    """On a frozen build, copy bundled defaults next to the exe on first run.

    layout.json and default-config.json ship inside the exe (read-only in _MEIPASS);
    the app needs writable copies beside the exe because layout.json is rewritten and
    the JSON files are the only record of what is programmed on the device.
    """
    if BUNDLE_DIR == HERE:
        return
    for name in ("layout.json", "default-config.json"):
        dst = os.path.join(HERE, name)
        if os.path.exists(dst):
            continue
        src = os.path.join(BUNDLE_DIR, name)
        if os.path.exists(src):
            try:
                shutil.copyfile(src, dst)
            except Exception:
                pass


def main():
    if "--version" in sys.argv:
        print("Macro Studio", __version__)
        return
    _seed_data_files()
    cfg = load_config()
    engine = Engine(cfg)
    engine.register_all()

    if "--selftest" in sys.argv:
        print("config:", CONFIG_PATH)
        print("macros:", list(cfg["macros"]))
        print("bindings:", cfg["bindings"])
        print("registered hotkeys:", list(engine._registered))
        print("chords:", CHORDS)
        engine.unregister_all()
        print("selftest OK")
        return

    enable_dpi_awareness()      # must precede the first window
    run_gui(cfg, engine)


if __name__ == "__main__":
    main()
