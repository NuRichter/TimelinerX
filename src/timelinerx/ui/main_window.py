from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QMainWindow, QMessageBox, QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget)

from .. import APP_NAME, RENDER_ENGINE_VERSION, __version__
from ..core.errors import TimelinerXError
from ..i18n import tr
from ..projects.project import Project
from .icons import brand_pixmap, icon
from .style import DARK, LIGHT, Tokens, stylesheet

_TOKENS: Tokens = LIGHT


def current_tokens() -> Tokens:
    return _TOKENS


def system_prefers_reduced_motion() -> bool:
    """Windows: 'Show animations in Windows' off (SPI_GETCLIENTAREAANIMATION)."""
    import sys
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        val = ctypes.c_int(1)
        ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(val), 0)
        return val.value == 0
    except Exception:  # noqa: BLE001
        return False


PAGES = ["dashboard", "import", "analysis", "journey", "visual", "video", "preview", "queue", "library",
         "settings", "about"]


class MainWindow(QMainWindow):
    def __init__(self, state):
        super().__init__()
        self.state = state
        self.setWindowTitle(f"{APP_NAME}")
        self.resize(1360, 880)
        self.setMinimumSize(QSize(1060, 680))
        self.apply_theme()
        self.apply_motion()

        from .pages.about import AboutPage
        from .pages.analysis import AnalysisPage
        from .pages.dashboard import DashboardPage
        from .pages.importer import ImportPage
        from .pages.journey import JourneyPage
        from .pages.library import LibraryPage
        from .pages.preview import PreviewPage
        from .pages.queue import QueuePage
        from .pages.settings import SettingsPage
        from .pages.video import VideoPage
        from .pages.visual import VisualPage

        root = QWidget()
        root.setObjectName("Root")
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(224)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(0, 0, 0, 12)
        brand = QLabel()
        brand.setObjectName("Brand")
        brand.setAccessibleName(APP_NAME)
        brand.setPixmap(brand_pixmap(_TOKENS is DARK, 172))
        self._brand = brand
        sub = QLabel(f"v{__version__} · {RENDER_ENGINE_VERSION}")
        sub.setObjectName("BrandSub")
        sv.addWidget(brand)
        sv.addWidget(sub)
        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        self.nav.setIconSize(QSize(18, 18))
        self.nav.setAccessibleName(tr("ui.nav"))
        icons = {"dashboard": "dashboard", "import": "import", "analysis": "analysis", "journey": "journey",
                 "visual": "visual", "video": "video", "preview": "preview", "queue": "queue",
                 "library": "library", "settings": "settings", "about": "about"}
        for key in PAGES:
            it = QListWidgetItem(icon(icons[key], _TOKENS.text_2), tr(f"ui.nav.{key}"))
            it.setData(Qt.UserRole, key)
            self.nav.addItem(it)
        sv.addWidget(self.nav, 1)
        # sidebar wide enough for the longest page name in this language (224–300 px)
        fm = self.nav.fontMetrics()
        longest = max(fm.horizontalAdvance(self.nav.item(i).text()) for i in range(self.nav.count()))
        side.setFixedWidth(max(224, min(300, longest + 18 + 10 + 20 + 16 + 24)))
        privacy = QLabel(tr("ui.local_only"))
        privacy.setProperty("muted", "true")
        privacy.setWordWrap(True)
        privacy.setContentsMargins(16, 0, 16, 0)
        sv.addWidget(privacy)
        h.addWidget(side)

        self.stack = QStackedWidget()
        self.pages = {
            "dashboard": DashboardPage(state, self.open_timeline, self.open_project),
            "import": ImportPage(state),
            "analysis": AnalysisPage(state),
            "journey": JourneyPage(state, self.save_project, self.open_project),
            "visual": VisualPage(state),
        }
        self.pages["video"] = VideoPage(state, self.enqueue_current)
        self.pages["preview"] = PreviewPage(state, self.pages["visual"].add_keyframe)
        self.pages["queue"] = QueuePage(state, self)
        self.pages["library"] = LibraryPage(state)
        self.pages["settings"] = SettingsPage(state, self.settings_changed)
        self.pages["about"] = AboutPage(state, self.reduced_motion)
        for key in PAGES:
            self.stack.addWidget(self.pages[key])
        h.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)
        state.navigate.connect(self.go)
        state.status.connect(lambda m: self.statusBar().showMessage(m, 6000))
        self.statusBar().showMessage(tr("ui.status.ready"))

        for key, seq in (("import", "Ctrl+I"), ("preview", "Ctrl+P"), ("queue", "Ctrl+R"), ("library", "Ctrl+L")):
            a = QAction(self)
            a.setShortcut(QKeySequence(seq))
            a.triggered.connect(lambda _=False, k=key: self.go(k))
            self.addAction(a)
        a = QAction(self)
        a.setShortcut(QKeySequence("Ctrl+S"))
        a.triggered.connect(self.save_project)
        self.addAction(a)
        self.tray = QSystemTrayIcon(icon("video", _TOKENS.accent, 32), self) if QSystemTrayIcon.isSystemTrayAvailable() else None
        if self.tray:
            self.tray.show()

    # ----------------------------------------------------------- appearance
    def apply_theme(self):
        global _TOKENS
        mode = self.state.settings.ui.theme
        if mode == "system":
            from PySide6.QtGui import QGuiApplication
            try:
                dark = QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
            except AttributeError:
                dark = False
        else:
            dark = mode == "dark"
        _TOKENS = DARK if dark else LIGHT
        from ..rendering.qt import families
        QApplication.instance().setStyleSheet(stylesheet(_TOKENS, families("ui")))
        if getattr(self, "_brand", None) is not None:
            self._brand.setPixmap(brand_pixmap(dark, 172))

    def apply_motion(self):
        """Honour reduced motion: disable Qt's animated combo/tooltip/menu effects."""
        pref = self.state.settings.ui.reduced_motion
        reduce = pref == "on" or (pref == "system" and system_prefers_reduced_motion())
        for eff in (Qt.UI_AnimateCombo, Qt.UI_AnimateMenu, Qt.UI_AnimateTooltip, Qt.UI_FadeMenu,
                    Qt.UI_FadeTooltip, Qt.UI_AnimateToolBox):
            QApplication.setEffectEnabled(eff, not reduce)
        self.reduced_motion = reduce

    def settings_changed(self):
        self.apply_motion()
        self.apply_theme()
        self.pages["queue"].apply_advanced()
        self.pages["about"].apply_theme()

    def go(self, key: str):
        if key == "tutorial":
            self.show_tutorial()
            return
        if key in PAGES:
            self.nav.setCurrentRow(PAGES.index(key))

    def show_tutorial(self):
        from .tutorial import TutorialDialog
        TutorialDialog(self, self.reduced_motion).exec()

    def notify(self, title: str, msg: str):
        if self.tray:
            self.tray.showMessage(title, msg, QSystemTrayIcon.Information, 6000)
        self.statusBar().showMessage(f"{title}: {msg}", 10000)

    # ---------------------------------------------------------------- files
    def open_timeline(self, path: str | None = None):
        if not path:
            self.go("import")
            self.pages["import"].choose()
            return
        self.go("import")
        self.pages["import"].load(path)

    def open_project(self):
        f, _ = QFileDialog.getOpenFileName(self, tr("ui.journey.open"), str(Path.home()),
                                           "TimelinerX project (*.nrproj)")
        if not f:
            return
        try:
            proj = Project.load(f)
        except TimelinerXError as e:
            QMessageBox.critical(self, tr("ui.journey.open"), str(e))
            return
        if proj.timeline.path and Path(proj.timeline.path).is_file() and proj.timeline.sha256:
            from ..timeline.parser import sha256_file
            if sha256_file(Path(proj.timeline.path)) != proj.timeline.sha256:
                if QMessageBox.question(self, tr("ui.journey.open"),
                                        tr("ui.journey.timeline_changed", path=proj.timeline.path)) != QMessageBox.Yes:
                    return
        self.state.set_project(proj, Path(f))
        if proj.timeline.path and Path(proj.timeline.path).is_file():
            self.pages["import"].load(proj.timeline.path)
            self.state.project.period = proj.period  # keep the saved period after import
        else:
            QMessageBox.warning(self, tr("ui.journey.open"), tr("ui.journey.timeline_missing",
                                                                path=proj.timeline.path))
        self.go("journey")

    def save_project(self):
        start = str(self.state.project_path or Path.home() / f"{self.state.project.name}.nrproj")
        f, _ = QFileDialog.getSaveFileName(self, tr("ui.journey.save"), start, "TimelinerX project (*.nrproj)")
        if f:
            p = self.state.project.save(f)
            self.state.project_path = p
            self.statusBar().showMessage(tr("ui.status.saved", path=str(p)), 6000)

    def enqueue_current(self, output: str):
        p = self.state.project
        errs = p.validate()
        if not p.timeline.path:
            errs.insert(0, tr("ui.video.need_timeline"))
        if errs:
            QMessageBox.warning(self, tr("ui.video.render"), "\n".join(errs))
            return
        if not output.lower().endswith(".mp4"):
            QMessageBox.warning(self, tr("ui.video.render"), tr("ui.video.need_mp4"))
            return
        if Path(output).exists():
            if QMessageBox.question(self, tr("ui.video.render"), tr("ui.video.overwrite", path=output)) != QMessageBox.Yes:
                return
            Path(output).unlink()
        self.pages["queue"].enqueue(p, output)
        self.go("queue")

    def closeEvent(self, e):
        if self.pages["queue"].busy():
            r = QMessageBox.question(self, APP_NAME, tr("ui.quit_while_rendering"))
            if r != QMessageBox.Yes:
                e.ignore()
                return
            self.pages["queue"].stop(keep=True)
            q = self.pages["queue"]
            if q.active and q.active.worker:
                q.active.worker.wait(15000)
        self.pages["preview"].shutdown()
        from ..encoding.ffmpeg import kill_all_children
        kill_all_children()
        e.accept()
