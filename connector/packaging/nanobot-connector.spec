# PyInstaller spec for the nanobot connector single-file executable.
# Build: pyinstaller packaging/nanobot-connector.spec
# Output: dist/nanobot-connector(.exe)

# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

a = Analysis(
    ["entry.py"],
    pathex=[".."],
    binaries=[],
    datas=[("../nanobot_connector/templates", "nanobot_connector/templates")],
    hiddenimports=[
        "nanobot_connector",
        "websockets",
        "typer",
        "pydantic",
        "tkinter",
        "keyring",
        "mss",
        "PIL",
        "pynput",
        *collect_submodules("mss"),
        *collect_submodules("PIL"),
        *collect_submodules("pynput"),
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="nanobot-connector",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    # Windows: sign the produced exe in CI to avoid SmartScreen/AV false positives.
    # codesign_identity is applied on macOS via the CI signing step.
)
