# PyInstaller — RuView Ultimate Desktop (Windows .exe)
# Запуск: cd desktop_app && pyinstaller ruview_desktop.spec

from PyInstaller.utils.hooks import collect_all

block_cipher = None

pdatas, pbinaries, phidden = collect_all("PyQt6")

a = Analysis(
    ["ruview_desktop.py"],
    pathex=[],
    binaries=pbinaries,
    datas=pdatas,
    hiddenimports=phidden
    + [
        "zeroconf",
        "zeroconf._protocol",
        "zeroconf._utils",
        "ifaddr",
        "websocket",
    ],
    hookspath=[],
    hooksconfig={},
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
    name="RuViewDesktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
