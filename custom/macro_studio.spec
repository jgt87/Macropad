# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_data_files

hid_bins = collect_dynamic_libs("hid")
hid_datas = collect_data_files("hid")

a = Analysis(
    ["macro_studio.py"],
    pathex=["."],
    binaries=hid_bins,
    datas=[("layout.json", "."), ("default-config.json", ".")] + hid_datas,
    hiddenimports=["macropad", "_version", "pystray._win32", "PIL.ImageTk"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MacroStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
