# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for /compute (onedir, windowed). Build from the repo root:
#   uv run pyinstaller --noconfirm --clean packaging/compute.spec
# PyInstaller cannot cross-compile: build the Mac app on a Mac and the Windows app on Windows.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))  # import compute from src/ even if the editable install is skipped

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

# Data files keep these relative paths under sys._MEIPASS (see compute/app/resources.py).
datas = [
    (str(SRC / "compute" / "app" / "static"), "compute/app/static"),
    (str(SRC / "compute" / "server" / "admin"), "compute/server/admin"),
]
terms = ROOT / "docs" / "TERMS.md"
if terms.exists():
    datas.append((str(terms), "docs"))
datas += collect_data_files("webview")

hiddenimports = [
    *collect_submodules("compute"),
    *collect_submodules("uvicorn"),
    *collect_submodules("zeroconf"),
    "webview",
    "multipart",
    "python_multipart",
]
if IS_MAC:
    hiddenimports += ["webview.platforms.cocoa", "objc", "Foundation", "AppKit", "WebKit"]
if IS_WIN:
    hiddenimports += [
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "clr",
        "clr_loader",
        "pythonnet",
    ]

a = Analysis(
    [str(SRC / "compute" / "__main__.py")],
    pathex=[str(SRC)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="compute",
    console=False,          # windowed: no terminal window
    argv_emulation=False,
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, name="compute", upx=False)

if IS_MAC:
    app = BUNDLE(
        coll,
        name="compute.app",
        bundle_identifier="ai.compute.app",
        info_plist={
            "CFBundleName": "compute",
            "CFBundleDisplayName": "/compute",
            "CFBundleShortVersionString": "0.2.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
            "NSLocalNetworkUsageDescription": "/compute finds and joins compute pools on your local network.",
            "NSBonjourServices": ["_compute._tcp"],
        },
    )
