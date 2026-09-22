from __future__ import annotations

import time

from PySide6.QtCore import QMutex, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider

from ...camera.planner import jerk_report
from ...core.errors import TimelinerXError
from ...core.geo import meters_to_latlon
from ...i18n import tr
from ...projects.project import PREVIEW_RESOLUTIONS, Project
from ...pipeline.preview import PreviewSession
from ..icons import icon
from ..widgets import Banner, Card, ScrollPage, muted, page_header


class AspectLabel(QLabel):
    """Keeps the preview at the project's aspect ratio and as large as the page allows."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.aspect = 16 / 9
        self._pm = None

    def set_aspect(self, a: float):
        self.aspect = a
        self.updateGeometry()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return int(min(w / self.aspect, 640))

    def sizeHint(self):
        return QSize(960, self.heightForWidth(960))

    def setFrame(self, pm):
        self._pm = pm
        self._rescale()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._rescale()

    def _rescale(self):
        if self._pm is not None:
            self.setPixmap(self._pm.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))


class PreviewRenderer(QThread):
    """Renders the most recently requested frame; stale requests are dropped."""

    frameReady = Signal(int, QImage)
    sessionReady = Signal(object)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mutex = QMutex()
        self._want = None
        self._project = None
        self._res = "720p"
        self.session = None
        self._stop = False

    def configure(self, project: Project, res: str):
        self._mutex.lock()
        self._project = Project.from_dict(project.to_dict())
        self._res = res
        self.session = None
        self._mutex.unlock()

    def request(self, frame: int):
        self._mutex.lock()
        self._want = frame
        self._mutex.unlock()

    def stop(self):
        self._stop = True

    def run(self):
        while not self._stop:
            self._mutex.lock()
            want, proj, res, sess = self._want, self._project, self._res, self.session
            self._want = None
            self._mutex.unlock()
            if proj is not None and sess is None:
                try:
                    sess = PreviewSession(proj, res, allow_placeholder=True)
                    self._mutex.lock()
                    self.session = sess
                    self._mutex.unlock()
                    self.sessionReady.emit(sess)
                except Exception as e:  # noqa: BLE001
                    msg = str(e) + (f"\n{e.hint}" if isinstance(e, TimelinerXError) and e.hint else "")
                    self.failed.emit(msg)
                    self._mutex.lock()
                    self._project = None
                    self._mutex.unlock()
                    continue
            if want is None or sess is None:
                self.msleep(15)
                continue
            try:
                img = sess.render_frame(want)
                self.frameReady.emit(want, img.copy())
            except Exception as e:  # noqa: BLE001
                self.failed.emit(str(e))


class PreviewPage(ScrollPage):
    def __init__(self, state, add_keyframe):
        super().__init__()
        self.state = state
        self.add_keyframe_cb = add_keyframe
        self.lay.addWidget(page_header(tr("ui.preview.title"), tr("ui.preview.subtitle")))
        top = QHBoxLayout()
        self.res = QComboBox()
        for r in PREVIEW_RESOLUTIONS:
            self.res.addItem(r, r)
        self.res.setCurrentIndex(1)
        self.res.setAccessibleName(tr("ui.preview.resolution"))
        top.addWidget(QLabel(tr("ui.preview.resolution")))
        top.addWidget(self.res)
        self.refresh_btn = QPushButton(tr("ui.preview.refresh"))
        top.addWidget(self.refresh_btn)
        top.addStretch(1)
        self.lay.addLayout(top)
        self.lay.addWidget(muted(tr("ui.preview.limit_note")))
        self.banner = Banner("info", tr("ui.preview.idle"))
        self.lay.addWidget(self.banner)
        card = Card()
        self.view = AspectLabel()
        self.view.setAlignment(Qt.AlignCenter)
        self.view.setMinimumSize(QSize(320, 180))
        sp = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        sp.setHeightForWidth(True)
        self.view.setSizePolicy(sp)
        self.view.setAccessibleName(tr("ui.preview.frame"))
        card.add(self.view)
        ctl = QHBoxLayout()
        self.play = QPushButton()
        self.play.setIcon(icon("play"))
        self.play.setAccessibleName(tr("ui.preview.play"))
        self.play.setCheckable(True)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setAccessibleName(tr("ui.preview.scrub"))
        self.pos = QLabel("0.0 s")
        self.kf_btn = QPushButton(tr("ui.preview.add_keyframe"))
        ctl.addWidget(self.play)
        ctl.addWidget(self.slider, 1)
        ctl.addWidget(self.pos)
        ctl.addWidget(self.kf_btn)
        card.add(ctl)
        self.info = muted("")
        card.add(self.info)
        self.lay.addWidget(card)
        self.finish()

        self.worker = PreviewRenderer(self)
        self.worker.frameReady.connect(self.show_frame)
        self.worker.sessionReady.connect(self.session_ready)
        self.worker.failed.connect(lambda m: self.banner.set("danger", m))
        self.worker.start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.play.toggled.connect(self.toggle_play)
        self.slider.valueChanged.connect(self.seek)
        self.refresh_btn.clicked.connect(self.reconfigure)
        self.res.currentIndexChanged.connect(self.reconfigure)
        self.kf_btn.clicked.connect(self.add_keyframe)
        self._dirty = True
        self._last_frame_t = None
        state.projectChanged.connect(self.mark_dirty)

    def mark_dirty(self):
        self._dirty = True
        if self.isVisible():
            self.banner.set("info", tr("ui.preview.changed"))

    def showEvent(self, e):
        super().showEvent(e)
        if self._dirty:
            self.reconfigure()

    def reconfigure(self):
        if not self.state.project.timeline.path:
            self.banner.set("info", tr("ui.preview.need_timeline"))
            return
        self._dirty = False
        self.banner.set("info", tr("ui.preview.preparing"))
        self.worker.configure(self.state.project, self.res.currentData())

    def session_ready(self, sess):
        self.slider.blockSignals(True)
        self.slider.setRange(0, sess.plan.frame_count - 1)
        self.slider.blockSignals(False)
        w, h = self.state.project.video.dimensions()
        self.view.set_aspect(w / h)
        self.view.setMinimumHeight(self.view.heightForWidth(max(320, self.view.width())))
        rep = jerk_report(sess.plan)
        self.info.setText(tr("ui.preview.info", pw=sess.width, ph=sess.height, w=w, h=h,
                             frames=sess.plan.frame_count, ax=f"{rep['max_accel_x_vp']:.4f}",
                             az=f"{rep['max_accel_zoom']:.4f}"))
        if getattr(sess, "notice", None) == "carto_key_missing":
            self.banner.set("warn", tr("ui.preview.carto_key_missing"))
        else:
            self.banner.set("ok", tr("ui.preview.ready"))
        self.worker.request(self.slider.value())

    def seek(self, v):
        fps = self.state.project.video.fps
        self.pos.setText(f"{v / fps:.1f} s")
        self.worker.request(v)

    def show_frame(self, idx, img: QImage):
        self.view.setFrame(QPixmap.fromImage(img))
        s = self.worker.session
        if s is not None and s.missing_tiles:
            self.banner.set("warn", tr("ui.preview.missing_tiles", n=s.missing_tiles))

    def toggle_play(self, on):
        self.play.setIcon(icon("pause" if on else "play"))
        if on:
            self._t0 = time.perf_counter()
            self._f0 = self.slider.value()
            self.timer.start(33)
        else:
            self.timer.stop()

    def tick(self):
        fps = self.state.project.video.fps
        f = self._f0 + int((time.perf_counter() - self._t0) * fps)
        if f > self.slider.maximum():
            self.play.setChecked(False)
            return
        self.slider.setValue(f)

    def add_keyframe(self):
        s = self.worker.session
        if s is None:
            return
        f = self.slider.value()
        lat, lon = meters_to_latlon(float(s.plan.cx[f]), float(s.plan.cy[f]))
        import math
        span_km = float(s.plan.span_y[f]) * math.cos(math.radians(lat)) / 1000.0
        self.add_keyframe_cb(f / s.plan.fps, lat, lon, span_km)
        self.banner.set("ok", tr("ui.preview.keyframe_added", t=f"{f / s.plan.fps:.1f}"))

    def shutdown(self):
        self.worker.stop()
        self.worker.wait(3000)
