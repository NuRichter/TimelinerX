"""GUI smoke test (offscreen): every page builds, navigation works, import and preview run."""

import time

import pytest

pytest.importorskip("PySide6.QtWidgets")


def _spin(app, sec):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def test_main_window_pages(fix):
    from PySide6.QtWidgets import QApplication

    from timelinerx.storage.settings import AppSettings
    from timelinerx.ui.main_window import PAGES, MainWindow
    from timelinerx.ui.state import AppState

    app = QApplication.instance()
    if not isinstance(app, QApplication):
        pytest.skip("a QGuiApplication (not QApplication) already exists in this process")
    s = AppSettings.load()
    s.ui.advanced_diagnostics = True
    st = AppState(s)
    w = MainWindow(st)
    w.show()
    for key in PAGES:
        w.go(key)
        _spin(app, 0.1)
        assert w.stack.currentWidget() is w.pages[key]
    w.pages["import"].load(str(fix / "small.json"))
    t0 = time.time()
    while st.timeline is None and time.time() - t0 < 30:
        _spin(app, 0.1)
    assert st.timeline is not None and st.project.timeline.sha256
    st.project.visual.map_provider = "plain"
    w.go("preview")
    t0 = time.time()
    while w.pages["preview"].worker.session is None and time.time() - t0 < 60:
        _spin(app, 0.1)
    assert w.pages["preview"].worker.session is not None
    w.pages["preview"].shutdown()
    w.close()
