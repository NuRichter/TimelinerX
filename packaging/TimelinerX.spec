# PyInstaller spec for TimelinerX.
#   pyinstaller packaging/TimelinerX.spec --noconfirm
# Environment variables:
#   TLX_ONEDIR=1            build a folder instead of a single .exe (easier LGPL re-linking of Qt)
#   TLX_FFMPEG_DIR=<dir>    bundle ffmpeg(.exe) + ffprobe(.exe) from <dir> (see THIRD_PARTY_NOTICES)
#   TLX_CONSOLE=1           keep a console window (debug builds)
import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"
IS_WIN = sys.platform == "win32"
exe_ext = ".exe" if IS_WIN else ""

datas = [
    (str(ROOT / "assets" / "themes"), "assets/themes"),
    (str(ROOT / "assets" / "fonts"), "assets/fonts"),
    (str(ROOT / "assets" / "icons"), "assets/icons"),
    (str(ROOT / "assets" / "brand" / "mark.png"), "assets/brand"),
    (str(ROOT / "assets" / "brand" / "mark_dark.png"), "assets/brand"),
    (str(ROOT / "assets" / "brand" / "wordmark_dark.png"), "assets/brand"),
    (str(ROOT / "assets" / "brand" / "wordmark_light.png"), "assets/brand"),
    (str(ROOT / "assets" / "brand" / "lockup_dark.png"), "assets/brand"),
    (str(ROOT / "assets" / "brand" / "lockup_light.png"), "assets/brand"),
    (str(ROOT / "assets" / "brand" / "mark_square.png"), "assets/brand"),
    *[(str(f), "timelinerx/i18n") for f in sorted((SRC / "timelinerx" / "i18n").glob("*.json"))],
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "."),
]
binaries = []
ff_dir = os.environ.get("TLX_FFMPEG_DIR")
if ff_dir:
    for tool in ("ffmpeg", "ffprobe"):
        p = Path(ff_dir) / (tool + exe_ext)
        if not p.is_file():
            raise SystemExit(f"TLX_FFMPEG_DIR set but {p} is missing")
        binaries.append((str(p), "ffmpeg"))

hidden = ["PySide6.QtSvg", "timelinerx.cli.main", "timelinerx.app.main"]
if (SRC / "timelinerx" / "_buildinfo.py").is_file():      # written by build_windows.ps1 (-CartoKey / carto_key.txt)
    hidden.append("timelinerx._buildinfo")
excludes = ["tkinter", "matplotlib", "scipy", "pytest", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
            "PySide6.Qt3DCore", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtCharts",
            "PySide6.QtDataVisualization", "PySide6.QtPdf", "PySide6.QtBluetooth", "PySide6.QtSensors"]

a = Analysis([str(ROOT / "packaging" / "launcher.py")], pathex=[str(SRC)], binaries=binaries, datas=datas,
             hiddenimports=hidden, excludes=excludes, noarchive=False)
pyz = PYZ(a.pure)
icon = str(ROOT / "assets" / "icons" / ("app.ico" if IS_WIN else "app.png"))
version = str(ROOT / "packaging" / "version_info.txt") if IS_WIN else None
console = bool(os.environ.get("TLX_CONSOLE"))

# Two executables from one analysis:
#   TimelinerX(.exe)   windowed desktop app (GUI subsystem on Windows)
#   timelinerx-cli(.exe) console CLI for headless/scripted renders (JSON lines, exit codes)
if os.environ.get("TLX_ONEDIR"):
    gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="TimelinerX", console=console,
              icon=icon, version=version, upx=False)
    cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="timelinerx-cli", console=True,
              icon=icon, version=version, upx=False)
    coll = COLLECT(gui, cli, a.binaries, a.datas, name="TimelinerX", upx=False)
else:
    gui = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="TimelinerX", console=console,
              icon=icon, version=version, upx=False, runtime_tmpdir=None)
    cli = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="timelinerx-cli", console=True,
              icon=icon, version=version, upx=False, runtime_tmpdir=None)
