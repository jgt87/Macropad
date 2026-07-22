import os, sys, json, types, tempfile, importlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import macropad as m
import macro_studio as ms


def test_keystroke_token_roundtrip():
    for token in ("ctrl+c", "ctrl+shift+s", "f13"):
        parsed = m.parse_keystroke(token)
        assert m.parse_keystroke(ms.keystroke_token(*parsed)) == parsed


def test_macro_as_keystrokes_basic():
    events = [
        {"src": "k", "e": "down", "n": "ctrl"},
        {"src": "k", "e": "down", "n": "c"},
        {"src": "k", "e": "up", "n": "c"},
        {"src": "k", "e": "up", "n": "ctrl"},
    ]
    assert ms.macro_as_keystrokes({"events": events, "app": ""}) == [
        (m.MOD_CTRL, m.KEYCODES["c"])]


def test_macro_as_keystrokes_rejects():
    key_event = {"src": "k", "e": "down", "n": "a"}
    assert ms.macro_as_keystrokes({"events": [key_event], "app": "notepad"}) is None
    assert ms.macro_as_keystrokes(
        {"events": [{"src": "m", "e": "down", "b": "left"}], "app": ""}) is None
    six_keys = [{"src": "k", "e": "down", "n": name} for name in "abcdef"]
    assert ms.macro_as_keystrokes({"events": six_keys, "app": ""}) is None


def test_serialize_events_normalizes():
    key_events = [
        types.SimpleNamespace(time=10.0, event_type="down", scan_code=1, name="a"),
        types.SimpleNamespace(time=10.5, event_type="up", scan_code=1, name="a"),
    ]
    mouse_event = {"src": "m", "t": 10.2, "e": "wheel", "d": 1}
    result = ms.serialize_events(key_events, [mouse_event])
    assert [event["t"] for event in result] == sorted(event["t"] for event in result)
    assert result[0]["t"] == 0.0


def test_export_import_roundtrip(tmp_path):
    cfg = {
        "macros": {"Copy": {"events": [], "app": ""}},
        "bindings": {"1": "Copy"},
        "shortcuts": {"1": "f13"},
    }
    path = tmp_path / "binds.json"
    ms.export_bundle(path, cfg)
    macros, bindings, shortcuts = ms.import_bundle(path)
    assert macros == cfg["macros"]
    assert bindings == cfg["bindings"]
    assert shortcuts == cfg["shortcuts"]


def test_auto_import_fill_only(tmp_path):
    old_here, old_config = ms.HERE, ms.CONFIG_PATH
    try:
        ms.HERE = str(tmp_path)
        ms.CONFIG_PATH = str(tmp_path / "macros.json")
        bundle = {
            "macro_studio_binds": 1,
            "macros": {
                "Existing": {"events": [{"original": False}], "app": ""},
                "Added": {"events": [], "app": ""},
            },
            "bindings": {"2": "Added"},
            "shortcuts": {"2": "f14"},
        }
        with open(tmp_path / "bundle.json", "w", encoding="utf-8") as f:
            json.dump(bundle, f)
        with open(tmp_path / "other.json", "w", encoding="utf-8") as f:
            json.dump({"not": "a bundle"}, f)
        cfg = {
            "macros": {"Existing": {"events": [{"original": True}], "app": ""}},
            "bindings": {},
            "shortcuts": {},
        }
        assert ms.auto_import_folder(cfg) == 1
        assert "Added" in cfg["macros"]
        assert cfg["macros"]["Existing"]["events"] == [{"original": True}]
        assert "not" not in cfg["macros"]
    finally:
        ms.HERE, ms.CONFIG_PATH = old_here, old_config


def test_load_config_migrates_profiles(tmp_path):
    old_config = ms.CONFIG_PATH
    try:
        path = tmp_path / "macros.json"
        legacy = {
            "macros": {"Legacy": {"events": [], "app": ""}},
            "bindings": {"1": "Legacy"},
            "shortcuts": {"1": "f13"},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(legacy, f)
        ms.CONFIG_PATH = str(path)
        cfg = ms.load_config()
        assert cfg["active_profile"] == "Default"
        assert "Default" in cfg["profiles"]
        assert cfg["profiles"]["Default"]["bindings"] == cfg["bindings"]
    finally:
        ms.CONFIG_PATH = old_config


def test_describe_event():
    keyboard_label = ms.describe_event({"src": "k", "n": "ctrl", "e": "down"})
    assert "ctrl" in keyboard_label and "down" in keyboard_label
    assert ms.describe_event({"src": "m", "e": "wheel", "d": 1})
    assert ms.describe_event({"src": "m", "e": "up", "b": "right"})


def test_save_config_preserves_profile_apps(tmp_path):
    old_config = ms.CONFIG_PATH
    try:
        path = tmp_path / "macros.json"
        ms.CONFIG_PATH = str(path)
        cfg = {
            "macros": {}, "bindings": {"1": "Copy"}, "shortcuts": {"1": "f13"},
            "ui": {},
            "active_profile": "Coding",
            "profiles": {"Coding": {"bindings": {"1": "Copy"}, "shortcuts": {"1": "f13"},
                                    "apps": ["code.exe"]}},
        }
        ms.save_config(cfg, backup=False)   # backup=False: don't touch the real backups dir
        reloaded = json.load(open(path, encoding="utf-8"))
        # the app-binding survives the active-profile sync, and live binds are folded in
        assert reloaded["profiles"]["Coding"]["apps"] == ["code.exe"]
        assert reloaded["profiles"]["Coding"]["bindings"] == {"1": "Copy"}
    finally:
        ms.CONFIG_PATH = old_config
