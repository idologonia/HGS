# PyInstaller spec for HOME GAME SERVER.  Build:  pyinstaller packaging/hgs.spec --noconfirm
# Result: dist/HGS/HGS.exe (one folder; --onedir starts faster and trips antivirus less than --onefile).
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"

hidden = collect_submodules("uvicorn") + collect_submodules("websockets") + [
    "hgs.adapters.html.adapter", "hgs.adapters.python.adapter", "hgs.adapters.process.adapter",
    "hgs_sdk", "hgs_sdk.runner", "hgs_sdk.connection", "hgs_sdk.logic", "hgs_sdk._ws",
]
if sys.platform == "win32":
    hidden += ["win32job", "win32api", "win32con", "win32crypt"]

datas = [
    (str(ROOT / "sdk" / "js" / "hgs.js"), "hgs/static"),                    # served at /hgs/sdk/hgs.js
    (str(SRC / "hgs" / "core" / "db" / "migrations"), "hgs/core/db/migrations"),
    (str(SRC / "hgs_sdk"), "hgs_sdk"),  # real .py files: copied into Python games' environments
]

a = Analysis(
    [str(ROOT / "packaging" / "hgs_entry.py")],
    pathex=[str(SRC)],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
              "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia", "pytest", "playwright"],
    noarchive=False,
)
pyz = PYZ(a.pure)
icon = ROOT / "packaging" / "hgs.ico"
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="HGS",
    console=False,  # GUI app; the core and workers it starts get pipes for their output
    icon=str(icon) if icon.exists() else None,
    version=str(ROOT / "packaging" / "version_info.txt") if sys.platform == "win32" else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="HGS")
