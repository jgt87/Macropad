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
import subprocess
import sys
import threading
import time

import keyboard

try:
    import mouse                      # companion to `keyboard`, same author
except ImportError:                   # keystroke-only mode; recording still works
    mouse = None

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
CONFIG_PATH = os.path.join(HERE, "macros.json")
LAYOUT_PATH = os.path.join(HERE, "layout.json")

FOCUS_DELAY = 0.35      # settle time after focusing an app that was already running
LAUNCH_DELAY = 1.2      # extra settle time after a cold start (first paint is not "ready")
LAUNCH_TIMEOUT = 30.0   # how long to wait for a launched app's first window
LAUNCH_POLL = 0.25      # how often to look for that window
FOREGROUND_POLL = 0.2   # how often to sample the foreground window while recording
MAX_EVENT_GAP = 2.0     # cap replayed idle time: thinking pauses shouldn't be re-lived

# Each macropad key number -> the "exotic" key it sends and we listen for.
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
    cfg.setdefault("bindings", {})    # keynum -> macro name
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


def record_in_layout(keynum, macro_name):
    """Point layout.json's entry for `keynum` at the chord Bind just programmed.

    The firmware cannot report its own assignments, so layout.json is the only record of
    what is actually on the device. A bind that reprograms a key without updating it
    silently invalidates that record — and the next `macropad.py apply layout.json` would
    quietly undo the bind.

    The file is hand-maintained (aligned columns, per-key notes), so rewrite only the one
    entry's line instead of re-dumping the document. Returns a short status string.

    Every failure is reported, never raised: this runs after the flash write has already
    succeeded, and a bookkeeping problem must not look like a failed bind."""
    if keynum not in CHORDS:
        return "layout.json not updated (unknown key %s)" % keynum
    try:
        with open(LAYOUT_PATH, "r", encoding="utf-8") as f:
            text = f.read()
        json.loads(text)                      # refuse to touch a file we can't parse
    except Exception as e:
        return "layout.json not updated (%s)" % e

    entry = ('{ "type": "key", "keys": "%s", "note": "Macro Studio: %s" }'
             % (chord_macropad_token(keynum), macro_name.replace('"', "'")))
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


def _window_origin(hwnd):
    """Top-left of a window in screen coordinates, or None.

    Clicks are stored relative to this so a macro still lands on the right control when
    the app reopens somewhere else on screen."""
    if not hwnd:
        return None
    r = _RECT()
    if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r)):
        return None
    return r.left, r.top


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


def play_events(events, origin=None):
    """Replay a merged macro, preserving the original gaps between events.

    Written out rather than delegating to keyboard.play() because that can only replay
    keystrokes; interleaving clicks needs one loop driving both. `origin` is the target
    window's current top-left: when a click was recorded relative to a window, it is
    re-anchored here, so the macro follows the app rather than clicking blind screen
    coordinates."""
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
        if x is None or y is None:
            continue
        mouse.move(x, y)
        # 'double' is a press too (its release is the following 'up'). New recordings never
        # store it - see MouseRecorder - but older ones might, so keep it a single press.
        if d["e"] in ("down", "double"):
            mouse.press(d.get("b", "left"))
        elif d["e"] == "up":
            mouse.release(d.get("b", "left"))


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
        self.unregister_all()
        for keynum, macro_name in self.cfg["bindings"].items():
            if macro_name in self.cfg["macros"] and keynum in CHORDS:
                self._register(keynum, macro_name)

    def _register(self, keynum, macro_name):
        chord = CHORDS[keynum]
        handle = keyboard.add_hotkey(chord, self._run, args=(macro_name,),
                                     suppress=False, trigger_on_release=False)
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
        if app:
            label = macro.get("app_exe") or os.path.basename(_split_command(app)[0])
            if not ensure_app(app, macro.get("app_pid", 0), macro.get("app_exe", "")):
                # Focus could not be confirmed; typing now would hit the wrong window.
                self._status("Could not focus %s - macro not run" % label)
                return
            # where the window sits *now*, so recorded clicks can be re-anchored to it
            exe = macro.get("app_exe") or _split_command(app)[0]
            hwnd = _find_window_by_exe(exe)
            origin = _window_origin(hwnd)
            # final guard: focus can slip between ensure_app and here
            if not window_is_foreground(hwnd):
                self._status("Lost focus on %s - macro not run" % label)
                return
        # clear the trigger key's modifiers so they don't contaminate the macro
        release_modifiers()
        try:
            play_events(macro["events"], origin)
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
    ),
    "dark": dict(
        bg="#202020", surface="#2b2b2b", border="#383838", text="#ffffff",
        muted="#c5c5c5", accent="#4cc2ff", accent_hover="#63cbff", accent_text="#000000",
        btn="#2d2d2d", btn_hover="#363636", btn_press="#3d3d3d", btn_border="#414141",
        sel="#4cc2ff", sel_text="#000000",
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


def _use_dark_titlebar(root, dark):
    """Ask DWM for a dark title bar so the frame matches the content."""
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        # 20 on current Windows; 19 on early Win10 builds. Try both, ignore failure.
        for attr in (20, 19):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), ctypes.c_int(attr),
                ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


def _pick_font(tkfont, candidates, size, weight="normal"):
    families = set(tkfont.families())
    for name in candidates:
        if name in families:
            return (name, size, weight)
    return ("Segoe UI", size, weight)


def apply_theme(root):
    """Style ttk from scratch on the 'clam' base and return (palette, fonts).

    'clam' rather than the native 'vista' theme: vista widgets ignore any attempt to
    recolour them, so they'd stay light while the rest of the window went dark. Styling a
    neutral theme by hand is what makes one coherent look possible in both modes."""
    import tkinter.font as tkfont
    from tkinter import ttk

    dark = not _system_uses_light_theme()
    p = PALETTES["dark" if dark else "light"]
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

    style.configure("TButton", background=p["btn"], foreground=p["text"], font=body,
                    borderwidth=1, relief="flat", padding=(14, 7),
                    bordercolor=p["btn_border"], lightcolor=p["btn"], darkcolor=p["btn"])
    style.map("TButton",
              background=[("pressed", p["btn_press"]), ("active", p["btn_hover"])],
              bordercolor=[("active", p["btn_border"])],
              lightcolor=[("pressed", p["btn_press"]), ("active", p["btn_hover"])],
              darkcolor=[("pressed", p["btn_press"]), ("active", p["btn_hover"])])

    style.configure("Accent.TButton", background=p["accent"], foreground=p["accent_text"],
                    bordercolor=p["accent"], lightcolor=p["accent"], darkcolor=p["accent"])
    style.map("Accent.TButton",
              background=[("pressed", p["accent"]), ("active", p["accent_hover"])],
              foreground=[("pressed", p["accent_text"]), ("active", p["accent_text"])],
              lightcolor=[("active", p["accent_hover"])],
              darkcolor=[("active", p["accent_hover"])],
              bordercolor=[("active", p["accent_hover"])])

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
    from tkinter import messagebox, simpledialog, ttk

    root = tk.Tk()
    root.title("Macro Studio")
    p, fonts = apply_theme(root)
    root.geometry("960x540")
    root.minsize(760, 420)

    recording = {"events": None, "hook": None, "watcher": None, "mouse": None}

    # --- layout -------------------------------------------------------------
    # Pack order is load-bearing. Each pack() carves a slab off the remaining cavity, so
    # the full-width status bar must be claimed BEFORE the side-by-side panes; otherwise
    # the panes take the whole height and the status bar lands in the leftover strip down
    # the right-hand side.
    header = ttk.Frame(root, padding=(20, 16, 20, 8))
    header.pack(side="top", fill="x")
    ttk.Label(header, text="Macro Studio", style="Title.TLabel").pack(anchor="w")
    ttk.Label(header, text="Record a macro, then bind it to a key on the macropad.",
              style="Muted.TLabel").pack(anchor="w", pady=(2, 0))

    bar = ttk.Frame(root, padding=(20, 0, 20, 12))
    bar.pack(side="top", fill="x")

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
    key_card, key_list = make_table(panes, ("Key", "Bound macro"), (140, 180))
    key_card.grid(row=1, column=1, sticky="nsew", padx=(16, 0))

    def set_status(msg):
        status.config(text=msg)

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
            key_list.insert("", "end", iid=keynum,
                            values=(KEY_LABELS[keynum], cfg["bindings"].get(keynum, "—")))

        for tree, sel in ((macro_list, macro_sel), (key_list, key_sel)):
            keep = [i for i in sel if tree.exists(i)]
            if keep:
                tree.selection_set(keep)

    def selected_macro():
        sel = macro_list.selection()
        return sel[0] if sel else None

    def selected_keynum():
        sel = key_list.selection()
        return sel[0] if sel else None

    # --- recording ---
    def record_new():
        name = simpledialog.askstring("Record macro", "Name for this macro:", parent=root)
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

    def delete_macro():
        name = selected_macro()
        if not name:
            return
        if messagebox.askyesno("Delete", f"Delete macro '{name}'?"):
            cfg["macros"].pop(name, None)
            for k, v in list(cfg["bindings"].items()):
                if v == name:
                    cfg["bindings"].pop(k)
            save_config(cfg); engine.register_all(); refresh()
            set_status(f"Deleted '{name}'")

    def set_app():
        name = selected_macro()
        if not name:
            set_status("Select a macro first"); return
        cur = cfg["macros"][name].get("app", "")
        app = simpledialog.askstring(
            "Application context",
            "App to launch/focus before running (exe name, full path, or URL).\n"
            "Leave blank for none.\nExamples:  code   |   chrome   |   notepad.exe",
            initialvalue=cur, parent=root)
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
        name = selected_macro(); keynum = selected_keynum()
        if not name or not keynum:
            set_status("Select a macro AND a key"); return
        cfg["bindings"][keynum] = name
        save_config(cfg); engine.register_all(); refresh()
        # Binding is only half the job: the listener above waits for the chord, but the
        # physical key sends nothing until the device is programmed to emit it. Doing both
        # here is what makes "Bind" mean what the user expects.
        try:
            program_key_on_device(keynum)
        except Exception as e:
            set_status(f"Bound '{name}' to {KEY_LABELS[keynum]}, but the macropad could not"
                       f" be programmed ({e}) - plug it in and use 'Program Key on Device'")
            return
        # only now is the device's real state known, so record it
        note = record_in_layout(keynum, name)
        set_status(f"Bound '{name}' to {KEY_LABELS[keynum]}, programmed it to send"
                   f" {chord_macropad_token(keynum)} - {note}")

    def unbind():
        keynum = selected_keynum()
        if not keynum:
            return
        cfg["bindings"].pop(keynum, None)
        save_config(cfg); engine.register_all(); refresh()
        set_status(f"Unbound {KEY_LABELS[keynum]}")

    def program_key():
        keynum = selected_keynum()
        if not keynum:
            set_status("Select a key first"); return
        try:
            program_key_on_device(keynum)
            note = record_in_layout(keynum, cfg["bindings"].get(keynum, "(unbound)"))
            set_status(f"Programmed {KEY_LABELS[keynum]} to send"
                       f" {chord_macropad_token(keynum)} - {note}")
        except Exception as e:
            messagebox.showerror("Device error",
                                 f"Could not program the macropad:\n{e}\n\nIs it plugged in?")

    # buttons (into the bar created above the panes). One accent button only, per Fluent:
    # recording is the primary action, everything else acts on an existing macro.
    ttk.Button(bar, text="Record new", style="Accent.TButton",
               command=record_new).pack(side="left", padx=(0, 8))
    for txt, fn in [("Delete", delete_macro), ("Set app…", set_app), ("Test", test_macro),
                    ("Bind to key", bind), ("Unbind", unbind),
                    ("Program key on device", program_key)]:
        ttk.Button(bar, text=txt, command=fn).pack(side="left", padx=(0, 8))

    refresh()

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

        menu = pystray.Menu(
            pystray.MenuItem("Open Macro Studio", show, default=True),
            pystray.MenuItem("Exit", quit_all),
        )
        icon = pystray.Icon("MacroStudio", img, "Macro Studio", menu)
        threading.Thread(target=icon.run, daemon=True).start()
        return icon

    def hide_to_tray():
        root.withdraw()
        set_status("Minimised to tray")

    root.protocol("WM_DELETE_WINDOW", hide_to_tray)
    start_tray()
    root.mainloop()


# ---------------------------------------------------------------- entry
def main():
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
