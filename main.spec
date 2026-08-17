# -*- mode: python ; coding: utf-8 -*-
#
# build with:  pyinstaller main.spec
#
# run this ON WINDOWS - pyinstaller does not cross-compile, so building
# on mac/linux produces a mac/linux binary, not a windows .exe.

import keyboard
import os

# keyboard reads its layout/data files from inside its own package
# dir at runtime - if that dir isn't bundled, global hotkeys (f6/f7/f8)
# silently fail on any machine that isn't the one you built on
keyboard_pkg_dir = os.path.dirname(keyboard.__file__)

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[
        # bundle the entire keyboard package data dir (layouts etc)
        (keyboard_pkg_dir, "keyboard"),
    ],
    hiddenimports=[
        "win32timezone",  # commonly missed by the pywin32 hook
        "win32com",
        "win32com.client",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PianoPlayer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # no terminal window behind the UI
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # deliberately NOT setting uac_admin=True here - the app has its
    # own in-app elevation overlay/prompt, forcing OS-level UAC on
    # launch would skip past that and always demand admin up front
    icon=None,               # put a path to a .ico here if you have one
)
