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
import subprocess
import sys
import threading
import time

import keyboard

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
CONFIG_PATH = os.path.join(HERE, "macros.json")

FOCUS_DELAY = 0.35      # settle time after focusing an app that was already running
LAUNCH_DELAY = 1.2      # extra settle time after a cold start (first paint is not "ready")
LAUNCH_TIMEOUT = 30.0   # how long to wait for a launched app's first window
LAUNCH_POLL = 0.25      # how often to look for that window
FOREGROUND_POLL = 0.2   # how often to sample the foreground window while recording

# Each macropad key number -> the exotic chord it will send / we listen for.
CHORDS = {
    "1":  "ctrl+alt+windows+f1",  "2":  "ctrl+alt+windows+f2",
    "3":  "ctrl+alt+windows+f3",  "4":  "ctrl+alt+windows+f4",
    "5":  "ctrl+alt+windows+f5",  "6":  "ctrl+alt+windows+f6",
    "13": "ctrl+alt+windows+f7",  "14": "ctrl+alt+windows+f8",
    "15": "ctrl+alt+windows+f9",
}
KEY_LABELS = {
    "1": "Key 1", "2": "Key 2", "3": "Key 3", "4": "Key 4", "5": "Key 5",
    "6": "Key 6", "13": "Knob turn left", "14": "Knob press", "15": "Knob turn right",
}
# macropad token form (win, not windows) for programming the device via macropad.py
def chord_macropad_token(keynum):
    return CHORDS[keynum].replace("windows", "win")


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
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------- win32 helpers (app context)
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_RESTORE = 9

# HWNDs are pointers: without these, ctypes' default c_int return truncates them on 64-bit.
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.GetForegroundWindow.argtypes = []

# Shell surfaces that briefly take focus while you click around. Never a macro target.
IGNORED_EXES = {
    "explorer.exe", "searchhost.exe", "searchapp.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "textinputhost.exe", "lockapp.exe",
}


def _pid_for_hwnd(hwnd):
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return pid.value


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


def foreground_app():
    """(pid, full exe path) of the foreground window. (0, '') for our own windows,
    shell surfaces, and anything we can't identify."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return 0, ""
    pid = _pid_for_hwnd(hwnd)
    if not pid or pid == os.getpid():
        return 0, ""
    path = _exe_path_for_pid(pid)
    if not path or os.path.basename(path).lower() in IGNORED_EXES:
        return 0, ""
    return pid, path


def _find_window(match):
    """First visible, titled window whose (pid, exe basename) satisfies `match`."""
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            pid = _pid_for_hwnd(hwnd)
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


def _focus(hwnd):
    user32.ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)
    # focus-steal workaround: tap ALT then set foreground
    try:
        keyboard.press_and_release("alt")
    except Exception:
        pass
    user32.SetForegroundWindow(ctypes.c_void_p(hwnd))


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


def ensure_app(command, pid=0):
    """Put the macro's application in the foreground, starting it if it isn't running.

    Tries, in order: the exact instance recorded with the macro (`pid`), any window of
    the same executable, then a cold start — waiting up to LAUNCH_TIMEOUT for its first
    window rather than guessing at a fixed delay. Returns True once it has focus."""
    if not command:
        return True
    target, _ = _split_command(command)
    exe = _normalise_exe(target)

    hwnd = None
    if pid and _exe_name_for_pid(pid) == exe:   # guards against a recycled pid
        hwnd = _find_window_by_pid(pid)
    if hwnd is None:
        hwnd = _find_window_by_exe(exe)
    if hwnd:
        _focus(hwnd)
        time.sleep(FOCUS_DELAY)
        return True

    if not _launch(command):
        return False
    deadline = time.time() + LAUNCH_TIMEOUT
    while time.time() < deadline:
        time.sleep(LAUNCH_POLL)
        hwnd = _find_window_by_exe(exe)
        if hwnd:
            _focus(hwnd)
            time.sleep(LAUNCH_DELAY)
            return True
    return False


class ForegroundWatcher(threading.Thread):
    """Samples the foreground window while a macro is being recorded, so the macro can
    remember which application the user clicked into. Our own windows are ignored, so
    the Recording overlay never registers as the target."""

    def __init__(self, interval=FOREGROUND_POLL):
        threading.Thread.__init__(self, daemon=True)
        self.interval = interval
        self.transitions = []          # (timestamp, exe path, pid), in the order seen
        self._stop = threading.Event()

    def run(self):
        last = None
        while not self._stop.is_set():
            pid, path = foreground_app()
            if path and path != last:
                self.transitions.append((time.time(), path, pid))
                last = path
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()

    def app_at(self, when=None):
        """(exe path, pid) of the app that had focus at `when` — normally the time of
        the first recorded keystroke, so switching away afterwards doesn't steal the
        binding. Falls back to the first app seen."""
        if not self.transitions:
            return "", 0
        chosen = self.transitions[0]
        if when:
            for entry in self.transitions:
                if entry[0] > when:
                    break
                chosen = entry
        return chosen[1], chosen[2]


# ---------------------------------------------------------------- macro engine
def serialize_events(events):
    out = []
    for e in events:
        out.append({"t": e.time, "e": e.event_type, "s": e.scan_code, "n": e.name})
    # normalise timestamps to start at 0
    if out:
        t0 = out[0]["t"] or 0
        for d in out:
            d["t"] = (d["t"] or t0) - t0
    return out


def deserialize_events(data):
    evs = []
    for d in data:
        evs.append(keyboard.KeyboardEvent(event_type=d["e"], scan_code=d["s"],
                                          name=d.get("n"), time=d.get("t")))
    return evs


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
        if app and not ensure_app(app, macro.get("app_pid", 0)):
            # Don't type into whatever happens to be focused instead.
            self._status("Could not start %s - macro not run" % os.path.basename(app))
            return
        # release the trigger modifiers so they don't contaminate the macro
        for mod in ("ctrl", "alt", "shift", "windows"):
            try:
                keyboard.release(mod)
            except Exception:
                pass
        try:
            keyboard.play(deserialize_events(macro["events"]))
        except Exception:
            pass


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


# ---------------------------------------------------------------- GUI
def run_gui(cfg, engine):
    import tkinter as tk
    from tkinter import messagebox, simpledialog

    root = tk.Tk()
    root.title("Macro Studio - macropad")
    root.geometry("880x460")

    recording = {"events": None, "hook": None, "watcher": None}

    # button bar: packed first so it sits above the panes, buttons filling from the left
    bar = tk.Frame(root); bar.pack(side="top", fill="x", padx=8, pady=(8, 0))

    # layout
    left = tk.Frame(root); left.pack(side="left", fill="both", expand=True, padx=8, pady=8)
    right = tk.Frame(root); right.pack(side="left", fill="both", expand=True, padx=8, pady=8)

    tk.Label(left, text="Macros").pack(anchor="w")
    macro_list = tk.Listbox(left); macro_list.pack(fill="both", expand=True)

    tk.Label(right, text="Macropad keys  ->  bound macro").pack(anchor="w")
    key_list = tk.Listbox(right); key_list.pack(fill="both", expand=True)

    status = tk.Label(root, text="", anchor="w", relief="sunken")
    status.pack(side="bottom", fill="x")

    def set_status(msg):
        status.config(text=msg)

    # playback runs on worker threads; marshal their status back onto the tk thread
    engine.status_cb = lambda msg: root.after(0, lambda: set_status(msg))

    def refresh():
        macro_list.delete(0, "end")
        for name in sorted(cfg["macros"]):
            app = cfg["macros"][name].get("app", "")
            label = os.path.basename(_split_command(app)[0]) if app else ""
            macro_list.insert("end", name + (f"   [app: {label}]" if label else ""))
        key_list.delete(0, "end")
        for keynum in ("1", "2", "3", "4", "5", "6", "13", "14", "15"):
            bound = cfg["bindings"].get(keynum, "-")
            key_list.insert("end", f"{KEY_LABELS[keynum]:16s} -> {bound}")

    def selected_macro():
        sel = macro_list.curselection()
        if not sel:
            return None
        return sorted(cfg["macros"])[sel[0]]

    def selected_keynum():
        sel = key_list.curselection()
        if not sel:
            return None
        return ("1", "2", "3", "4", "5", "6", "13", "14", "15")[sel[0]]

    # --- recording ---
    def record_new():
        name = simpledialog.askstring("Record macro", "Name for this macro:", parent=root)
        if not name:
            return
        overlay = tk.Toplevel(root)
        overlay.title("Recording")
        overlay.attributes("-topmost", True)
        overlay.geometry("340x140")
        tk.Label(overlay, text="Recording keystrokes...\n\nClick into the application you want this\n"
                               "macro to run in, type it, then click Stop.",
                 font=("Segoe UI", 10)).pack(pady=10)
        recording["events"] = []
        recording["hook"] = keyboard.hook(lambda e: recording["events"].append(e))
        recording["watcher"] = ForegroundWatcher()
        recording["watcher"].start()

        def stop():
            if recording["hook"]:
                keyboard.unhook(recording["hook"])
                recording["hook"] = None
            watcher, recording["watcher"] = recording["watcher"], None
            raw = recording["events"] or []
            if watcher:
                watcher.stop()
            # bind to whatever had focus when the first key landed
            app, app_pid = watcher.app_at(raw[0].time if raw else None) if watcher else ("", 0)
            events = serialize_events(raw)
            overlay.destroy()
            cfg["macros"][name] = {"events": events, "app": app, "app_pid": app_pid}
            save_config(cfg)
            refresh()
            if app:
                set_status(f"Saved '{name}' ({len(events)} events) -> {os.path.basename(app)}"
                           f" (pid {app_pid})")
            else:
                set_status(f"Saved '{name}' ({len(events)} events) - no app detected,"
                           f" use Set App... to pick one")

        tk.Button(overlay, text="Stop", width=12, command=stop).pack(pady=6)
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
        save_config(cfg); refresh()
        set_status(f"Set app context for '{name}'")

    def test_macro():
        name = selected_macro()
        if not name:
            set_status("Select a macro first"); return
        app = cfg["macros"][name].get("app", "")
        where = os.path.basename(_split_command(app)[0]) if app else "the focused window"
        set_status(f"Running '{name}' in 2s -> {where}")
        # via _run: playback threads off the GUI, which may block while an app starts
        root.after(2000, lambda: engine._run(name))

    def bind():
        name = selected_macro(); keynum = selected_keynum()
        if not name or not keynum:
            set_status("Select a macro AND a key"); return
        cfg["bindings"][keynum] = name
        save_config(cfg); engine.register_all(); refresh()
        set_status(f"Bound '{name}' to {KEY_LABELS[keynum]} (listening for {CHORDS[keynum]})")

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
            set_status(f"Programmed {KEY_LABELS[keynum]} to send {chord_macropad_token(keynum)}")
        except Exception as e:
            messagebox.showerror("Device error",
                                 f"Could not program the macropad:\n{e}\n\nIs it plugged in?")

    # buttons (into the bar created above the panes)
    for txt, fn in [("Record New", record_new), ("Delete", delete_macro),
                    ("Set App...", set_app), ("Test", test_macro),
                    ("Bind ->Key", bind), ("Unbind", unbind),
                    ("Program Key on Device", program_key)]:
        tk.Button(bar, text=txt, command=fn).pack(side="left", padx=(0, 6))

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

    run_gui(cfg, engine)


if __name__ == "__main__":
    main()
