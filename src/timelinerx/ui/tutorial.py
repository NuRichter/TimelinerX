"""Tutorial dialog: the import motion graphic with play/pause, scrubbing, platform choice and
MP4 export."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QPushButton, QSizePolicy, QSlider,
                               QVBoxLayout, QWidget)

from ..i18n import language, tr
from ..rendering.qt import family
from .icons import icon
from .tutorial_scene import Palette, TutorialScene
from .widgets import Banner, SegmentedOptions, muted
from .workers import Worker


class TutorialPlayer(QWidget):
    def __init__(self, reduced_motion: bool = False, parent=None):
        super().__init__(parent)
        self.setMinimumSize(QSize(640, 360))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAccessibleName(tr("ui.tutorial.title"))
        self.t = 0.0
        self.playing = not reduced_motion
        self._last = time.perf_counter()
        self.set_platform("android")
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self.on_time = None

    def set_platform(self, platform: str):
        from .main_window import DARK, current_tokens
        pal = Palette.dark() if current_tokens() is DARK else Palette.light()
        self.scene = TutorialScene(platform, pal, family("ui"), rtl=language() == "ar")
        self.t = 0.0
        self.update()

    def heightForWidth(self, w: int) -> int:
        return int(w * 9 / 16)

    def hasHeightForWidth(self) -> bool:
        return True

    def _tick(self):
        now = time.perf_counter()
        dt = now - self._last
        self._last = now
        if self.playing and self.isVisible():
            self.t = (self.t + dt) % self.scene.duration
            self.update()
            if self.on_time:
                self.on_time(self.t)

    def seek(self, t: float):
        self.t = max(0.0, min(self.scene.duration - 1e-3, t))
        self.update()
        if self.on_time:
            self.on_time(self.t)

    def paintEvent(self, _e):
        p = QPainter(self)
        self.scene.draw(p, self.t, self.width(), self.height())
        p.end()


class TutorialDialog(QDialog):
    def __init__(self, parent=None, reduced_motion: bool = False):
        super().__init__(parent)
        self.setWindowTitle(tr("ui.tutorial.title"))
        self.resize(1180, 860)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)
        lay.addWidget(muted(tr("ui.tutorial.intro")))
        self.platform = SegmentedOptions(tr("ui.tutorial.platform"),
                                         [("android", tr("tut.platform.android"), tr("ui.tutorial.android_note")),
                                          ("iphone", tr("tut.platform.iphone"), tr("ui.tutorial.iphone_note"))])
        self.platform.set_value("android")
        lay.addWidget(self.platform)
        self.player = TutorialPlayer(reduced_motion)
        lay.addWidget(self.player, 1)
        ctl = QHBoxLayout()
        self.play = QPushButton()
        self.play.setAccessibleName(tr("ui.tutorial.play_pause"))
        self.play.clicked.connect(self._toggle)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setAccessibleName(tr("ui.tutorial.position"))
        self.slider.sliderMoved.connect(lambda v: self.player.seek(v / 1000 * self.player.scene.duration))
        ctl.addWidget(self.play)
        ctl.addWidget(self.slider, 1)
        lay.addLayout(ctl)
        self.banner = Banner("info", "")
        self.banner.hide()
        lay.addWidget(self.banner)
        row = QHBoxLayout()
        self.export = QPushButton(tr("ui.tutorial.export"))
        self.export.clicked.connect(self._export)
        close = QPushButton(tr("ui.tutorial.close"))
        close.clicked.connect(self.accept)
        go = QPushButton(tr("ui.tutorial.go_import"))
        go.setProperty("primary", "true")
        go.clicked.connect(self._go_import)
        row.addWidget(self.export)
        row.addStretch(1)
        row.addWidget(close)
        row.addWidget(go)
        lay.addLayout(row)
        self.platform.changed.connect(self.player.set_platform)
        self.player.on_time = lambda t: (not self.slider.isSliderDown()) and self.slider.setValue(
            int(t / self.player.scene.duration * 1000))
        self._update_play()

    def _toggle(self):
        self.player.playing = not self.player.playing
        self._update_play()

    def _update_play(self):
        from .main_window import current_tokens
        self.play.setIcon(icon("pause" if self.player.playing else "play", current_tokens().text))

    def _go_import(self):
        parent = self.parent()
        self.accept()
        if parent is not None and hasattr(parent, "go"):
            parent.go("import")

    def _export(self):
        start = str(Path.home() / f"TimelinerX-import-guide-{self.platform.value()}.mp4")
        f, _ = QFileDialog.getSaveFileName(self, tr("ui.tutorial.export"), start, "MP4 (*.mp4)")
        if not f:
            return
        if not f.lower().endswith(".mp4"):
            f += ".mp4"
        platform = self.platform.value()
        self.export.setEnabled(False)
        self.banner.show()
        self.banner.set("info", tr("ui.tutorial.exporting", pct=0))
        from ..pipeline.tutorial_export import export_tutorial

        def job(w):
            return export_tutorial(Path(f), platform, language(),
                                   progress=lambda x: w.progress.emit(int(x * 100), ""))
        self._w = Worker(job, self)
        self._w.progress.connect(lambda pct, _m: self.banner.set("info", tr("ui.tutorial.exporting", pct=pct)))
        self._w.done.connect(lambda p: (self.banner.set("ok", tr("ui.tutorial.exported", path=str(p))),
                                        self.export.setEnabled(True)))
        self._w.failed.connect(lambda e: (self.banner.set("danger", str(e)), self.export.setEnabled(True)))
        self._w.start()


class TutorialCard(QWidget):
    """Compact card with a live, muted preview of the tutorial and a button to open it."""

    def __init__(self, state, reduced_motion: bool = False, parent=None):
        super().__init__(parent)
        from .widgets import Card, primary_button
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        card = Card()
        row = QHBoxLayout()
        row.setSpacing(18)
        self.thumb = TutorialPlayer(reduced_motion)
        self.thumb.setMinimumSize(QSize(320, 180))
        self.thumb.setMaximumSize(QSize(320, 180))
        self.thumb.setCursor(Qt.PointingHandCursor)
        self.thumb.mousePressEvent = lambda _e: state.navigate.emit("tutorial")
        row.addWidget(self.thumb)
        col = QVBoxLayout()
        from PySide6.QtWidgets import QLabel
        t = QLabel(tr("ui.tutorial.card_title"))
        t.setObjectName("SectionTitle")
        t.setWordWrap(True)
        col.addWidget(t)
        col.addWidget(muted(tr("ui.tutorial.card_body")))
        b = primary_button(tr("ui.tutorial.open"))
        b.clicked.connect(lambda: state.navigate.emit("tutorial"))
        brow = QHBoxLayout()
        brow.addWidget(b)
        brow.addStretch(1)
        col.addLayout(brow)
        col.addStretch(1)
        row.addLayout(col, 1)
        card.add(row)
        lay.addWidget(card)
