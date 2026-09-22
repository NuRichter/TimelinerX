"""GUI entry point."""

from __future__ import annotations

import sys
import threading


def _splash():
    """Brand splash while the main window is constructed (no artificial delay)."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap
    from PySide6.QtWidgets import QApplication, QSplashScreen

    from .. import AUTHOR_WORKSPACE, __version__
    from ..rendering.qt import family
    from ..utils.paths import assets_dir
    logo = QPixmap(str(assets_dir() / "brand" / "lockup_dark.png"))
    if logo.isNull():
        return None
    dpr = max(1.0, QApplication.instance().devicePixelRatio())
    w, h = 560, 300
    pm = QPixmap(int(w * dpr), int(h * dpr))
    pm.setDevicePixelRatio(dpr)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    g = QLinearGradient(0, 0, 0, h)
    g.setColorAt(0, QColor("#141c33"))
    g.setColorAt(1, QColor("#080c18"))
    p.fillRect(QRectF(0, 0, w, h), g)
    lw = 360
    lh = lw * logo.height() / logo.width()
    p.drawPixmap(QRectF((w - lw) / 2, 92, lw, lh), logo, QRectF(logo.rect()))
    f = QFont(family("ui"))
    f.setPixelSize(12)
    p.setFont(f)
    p.setPen(QColor("#8e9ab3"))
    p.drawText(QRectF(0, h - 44, w, 20), Qt.AlignCenter, f"v{__version__}  ·  Made with {AUTHOR_WORKSPACE}")
    p.end()
    s = QSplashScreen(pm, Qt.WindowStaysOnTopHint)
    s.show()
    QApplication.processEvents()
    return s


def main() -> int:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication

    from .. import APP_ID, APP_NAME
    from ..i18n import set_language
    from ..rendering.qt import ensure_qt_app
    from ..storage.settings import AppSettings
    from ..utils.logging_setup import setup_logging
    from ..utils.paths import logs_dir

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:  # noqa: BLE001
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("NuRichter Workspace")
    from PySide6.QtGui import QIcon
    from ..utils.paths import assets_dir
    app.setWindowIcon(QIcon(str(assets_dir() / "icons" / "app.png")))
    ensure_qt_app()   # registers bundled fonts on the main thread
    splash = _splash()
    settings = AppSettings.load()
    set_language(settings.ui.language)
    from ..i18n import is_rtl
    if is_rtl():
        app.setLayoutDirection(Qt.RightToLeft)
    from ..maps.keys import set_user_key
    set_user_key(settings.maps.carto_api_key)
    log_path = setup_logging(logs_dir(), settings.privacy.debug_log_coordinates)

    from ..plugins.loader import load_plugins
    load_plugins(load_python=settings.storage.load_python_plugins)

    from ..ui.main_window import MainWindow
    from ..ui.state import AppState
    from ..ui.workers import Worker

    state = AppState(settings)
    win = MainWindow(state)
    win.show()
    if splash is not None:
        splash.finish(win)

    def scan(_w):
        from ..environment.scan import scan as do_scan
        return do_scan(storage_path=settings.storage.output_dir or None,
                       ffmpeg_path=settings.rendering.ffmpeg_path or None)

    w = Worker(scan, win)

    def scanned(rep):
        state.env_report = rep
        state.envChanged.emit()

    w.done.connect(scanned)
    w.failed.connect(lambda e: state.status.emit(str(e)))
    QTimer.singleShot(200, w.start)

    watcher = None
    if settings.storage.watch_enabled and settings.storage.watch_folder and settings.storage.watch_preset_project:
        from ..pipeline.watch import WatchFolder
        from ..projects.project import Project
        from pathlib import Path
        try:
            wf = WatchFolder(Path(settings.storage.watch_folder), Project.load(settings.storage.watch_preset_project),
                             Path(settings.storage.output_dir or Path.home() / "Videos" / "TimelinerX"),
                             logs_dir() / "watch.log",
                             notify=lambda t, m: QTimer.singleShot(0, lambda: win.notify(t, m)))
            watcher = threading.Thread(target=wf.run_forever, daemon=True, name="tlx-watch")
            watcher.start()
            app.aboutToQuit.connect(lambda: wf.cancel.cancel("quit"))
        except Exception as e:  # noqa: BLE001
            state.status.emit(f"Watch folder disabled: {e}")
    rc = app.exec()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
