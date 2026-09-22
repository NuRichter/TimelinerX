from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import QDate, QTimer
from PySide6.QtWidgets import (QComboBox, QDateEdit, QFormLayout, QGridLayout, QHBoxLayout, QLineEdit,
                               QPushButton, QVBoxLayout)

from ...core.errors import TimelinerXError
from ...i18n import format_distance, format_number, tr
from ...journeys.journey import build_journey, build_legs, transfer_threshold_km
from ...pipeline.render_job import camera_config, journey_config
from ...timeline.modes import Mode
from ..journey_sketch import JourneySketch
from ..widgets import (Banner, Card, ScrollPage, SegmentedOptions, StatTile, muted, page_header,
                       primary_button)
from ..workers import Worker

ZOOM_STYLES = ("fixed", "balanced", "active", "close_up")
DETECTION = ("conservative", "balanced", "sensitive")
FRAMING = ("off", "balanced", "close")
PACING = ("natural", "balanced", "faster", "fastest")


def _qd(s):
    return QDate.fromString(s, "yyyy-MM-dd") if s else QDate.currentDate()


def _opts(prefix, keys):
    return [(k, tr(f"{prefix}.{k}"), tr(f"{prefix}.{k}.desc")) for k in keys]


class JourneyPage(ScrollPage):
    def __init__(self, state, save_project, open_project):
        super().__init__()
        self.state = state
        self.worker = None
        self._rec = None
        self.lay.addWidget(page_header(tr("ui.journey.title"), tr("ui.journey.subtitle")))

        proj = Card(tr("ui.journey.project"))
        f = QFormLayout()
        self.name = QLineEdit()
        self.name.setAccessibleName(tr("ui.journey.name"))
        f.addRow(tr("ui.journey.name"), self.name)
        proj.add(f)
        row = QHBoxLayout()
        bs = QPushButton(tr("ui.journey.save"))
        bs.clicked.connect(save_project)
        bo = QPushButton(tr("ui.journey.open"))
        bo.clicked.connect(open_project)
        row.addWidget(bs)
        row.addWidget(bo)
        row.addStretch(1)
        proj.add(row)
        self.lay.addWidget(proj)

        per = Card(tr("ui.journey.period"))
        pf = QFormLayout()
        self.preset = QComboBox()
        self.preset.setAccessibleName(tr("ui.journey.quick"))
        pf.addRow(tr("ui.journey.quick"), self.preset)
        self.start = QDateEdit()
        self.start.setCalendarPopup(True)
        self.start.setDisplayFormat("yyyy-MM-dd")
        self.end = QDateEdit()
        self.end.setCalendarPopup(True)
        self.end.setDisplayFormat("yyyy-MM-dd")
        pf.addRow(tr("ui.journey.start"), self.start)
        pf.addRow(tr("ui.journey.end"), self.end)
        per.add(pf)
        self.lay.addWidget(per)

        # ------------------------------------------------ the journey at a glance
        res = Card(tr("ui.journey.result"))
        body = QHBoxLayout()
        body.setSpacing(18)
        self.sketch = JourneySketch()
        body.addWidget(self.sketch, 3)
        tiles = QVBoxLayout()
        tiles.setSpacing(12)
        self.s = {k: StatTile(tr(f"ui.journey.stat.{k}")) for k in
                  ("distance", "trips", "flights", "days", "points", "outliers")}
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(12)
        for n, w in enumerate(self.s.values()):
            grid.addWidget(w, n // 2, n % 2)
        tiles.addLayout(grid)
        self.modes_lbl = muted("")
        tiles.addWidget(self.modes_lbl)
        tiles.addStretch(1)
        body.addLayout(tiles, 2)
        res.add(body)
        self.banner = Banner("info", tr("ui.journey.load_first"))
        res.add(self.banner)
        self.lay.addWidget(res)

        # ------------------------------------------------ camera & pacing
        cam = Card(tr("ui.journey.camera"))
        cam.add(muted(tr("ui.journey.camera_note")))
        cg = QGridLayout()
        cg.setHorizontalSpacing(24)
        cg.setVerticalSpacing(14)
        self.zoom = SegmentedOptions(tr("ui.camera.zoom_style"), _opts("ui.camera.zoom", ZOOM_STYLES))
        self.trips = SegmentedOptions(tr("ui.journey.trip_detection"), _opts("ui.journey.trips", DETECTION))
        self.framing = SegmentedOptions(tr("ui.camera.framing"), _opts("ui.camera.framing", FRAMING))
        self.pacing_lt = SegmentedOptions(tr("ui.camera.long_trip_pacing"), _opts("ui.camera.ltp", PACING))
        self._cam_grid = cg
        self._cam_cols = 0
        self._layout_camera(2)
        cam.add(cg)
        self._cam_card = cam
        self.pace = Banner("info", tr("ui.journey.pace_wait"))
        cam.add(self.pace)
        prow = QHBoxLayout()
        self.use_calm = QPushButton(tr("ui.journey.use_calm"))
        self.use_lively = QPushButton(tr("ui.journey.use_lively"))
        self.use_calm.clicked.connect(lambda: self._use_duration("comfortable_s"))
        self.use_lively.clicked.connect(lambda: self._use_duration("brisk_s"))
        for b in (self.use_calm, self.use_lively):
            b.setEnabled(False)
            prow.addWidget(b)
        prow.addStretch(1)
        cam.add(prow)
        self.lay.addWidget(cam)

        # ------------------------------------------------ data processing
        opt = Card(tr("ui.journey.processing"))
        of = QFormLayout()
        self.source = QComboBox()
        for k in ("semantic", "detailed"):
            self.source.addItem(tr(f"ui.journey.source.{k}"), k)
        self.outliers = QComboBox()
        for k in ("conservative", "off"):
            self.outliers.addItem(tr(f"ui.journey.outliers.{k}"), k)
        of.addRow(tr("ui.journey.source"), self.source)
        of.addRow(tr("ui.journey.outliers"), self.outliers)
        opt.add(of)
        opt.add(muted(tr("ui.journey.processing_note")))
        self.lay.addWidget(opt)

        nb = primary_button(tr("ui.journey.next"))
        nb.clicked.connect(lambda: state.navigate.emit("visual"))
        r3 = QHBoxLayout()
        r3.addWidget(nb)
        r3.addStretch(1)
        self.lay.addLayout(r3)
        self.finish()

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(350)
        self._timer.timeout.connect(self.recompute)
        self._loading = False
        for w in (self.start, self.end):
            w.dateChanged.connect(self.changed)
        for w in (self.source, self.outliers):
            w.currentIndexChanged.connect(self.changed)
        self.trips.changed.connect(self._detection_changed)
        for w in (self.zoom, self.framing, self.pacing_lt):
            w.changed.connect(self._camera_changed)
        self.name.textEdited.connect(self.changed)
        self.preset.activated.connect(self.apply_preset)
        state.projectChanged.connect(self.load_from_project)
        state.timelineChanged.connect(self.fill_presets)
        state.settingsChanged.connect(lambda: self._timer.start())
        self.load_from_project()

    def _layout_camera(self, cols: int):
        """Two columns when the option rows fit side by side, otherwise one (long labels in
        some languages); never a horizontal scrollbar."""
        if cols == self._cam_cols:
            return
        g = self._cam_grid
        for w in (self.zoom, self.trips, self.framing, self.pacing_lt):
            g.removeWidget(w)
        for i, w in enumerate((self.zoom, self.trips, self.framing, self.pacing_lt)):
            g.addWidget(w, i // cols, i % cols)
        for c in range(2):
            g.setColumnStretch(c, 1 if c < cols else 0)
        self._cam_cols = cols

    def resizeEvent(self, e):
        super().resizeEvent(e)
        need = max(w.sizeHint().width() for w in (self.zoom, self.trips, self.framing, self.pacing_lt))
        avail = self.viewport().width() - 64 - 36
        self._layout_camera(2 if 2 * need + 24 <= avail else 1)

    def fill_presets(self):
        self.preset.clear()
        self.preset.addItem(tr("ui.journey.preset.custom"), None)
        tl = self.state.timeline
        if tl is None:
            return
        rng = tl.date_range()
        if not rng:
            return
        self.preset.addItem(tr("ui.journey.preset.all"), (str(rng[0]), str(rng[1])))
        for y in range(rng[1].year, rng[0].year - 1, -1):
            self.preset.addItem(str(y), (f"{y}-01-01", f"{y}-12-31"))

    def apply_preset(self, i):
        v = self.preset.itemData(i)
        if v:
            self.start.setDate(_qd(v[0]))
            self.end.setDate(_qd(v[1]))

    def load_from_project(self):
        self._loading = True
        p = self.state.project
        self.name.setText(p.name)
        self.start.setDate(_qd(p.period.start))
        self.end.setDate(_qd(p.period.end))
        self.source.setCurrentIndex(max(0, self.source.findData(p.journey.route_source)))
        self.outliers.setCurrentIndex(max(0, self.outliers.findData(p.journey.outlier_filter)))
        self.trips.set_value(p.journey.trip_detection)
        self.zoom.set_value(p.camera.mode)
        self.framing.set_value(p.camera.local_framing)
        self.pacing_lt.set_value(p.camera.compression)
        self._loading = False
        self._show_pace()
        self._timer.start()

    def changed(self, *_):
        if self._loading:
            return
        p = self.state.project
        p.name = self.name.text().strip() or "Untitled journey"
        p.period.start = self.start.date().toString("yyyy-MM-dd")
        p.period.end = self.end.date().toString("yyyy-MM-dd")
        p.journey.route_source = self.source.currentData()
        p.journey.outlier_filter = self.outliers.currentData()
        self._timer.start()

    def _detection_changed(self, key):
        if self._loading:
            return
        self.state.project.journey.trip_detection = key
        self.sketch.set_detection(key)
        self.state.touch_project()

    def _camera_changed(self, _key):
        if self._loading:
            return
        c = self.state.project.camera
        c.mode = self.zoom.value()
        c.local_framing = self.framing.value()
        c.compression = self.pacing_lt.value()
        self.state.touch_project()

    def _use_duration(self, which):
        if not self._rec:
            return
        self.state.project.video.duration_s = float(max(5.0, min(1800.0, round(self._rec[which]))))
        self.state.touch_project()

    def recompute(self):
        tl = self.state.timeline
        if tl is None:
            if self.state.project.timeline.path:
                self.banner.set("info", tr("ui.journey.timeline_not_loaded",
                                           name=Path(self.state.project.timeline.path).name))
            return
        proj = self.state.project
        if self.worker and self.worker.isRunning():
            self._timer.start()
            return
        vw, vh = 1920, 1080
        try:
            vw, vh = proj.video.dimensions()
        except TimelinerXError:
            pass

        def job(_w):
            from ...camera.planner import recommend_duration
            j = build_journey(tl, journey_config(proj))
            return j, recommend_duration(j, camera_config(proj), vw, vh)

        self.worker = Worker(job, self)
        self.worker.done.connect(lambda res: self.show(*res))
        self.worker.failed.connect(lambda e: self.banner.set(
            "warn", str(e) + (f"\n{e.hint}" if isinstance(e, TimelinerXError) and e.hint else "")))
        self.worker.start()

    def _unit(self):
        return self.state.settings.ui.distance_unit

    def show(self, j, rec=None):
        unit = self._unit()
        self.s["points"].set(format_number(j.stats["points"]))
        self.s["distance"].set(format_distance(j.total_km, unit))
        self.s["trips"].set(format_number(j.trip_count))
        self.s["flights"].set(format_number(len(j.arcs)))
        self.s["days"].set(format_number(j.stats["days"]))
        self.s["outliers"].set(format_number(j.outliers_removed))
        mk = j.stats.get("mode_km", {})
        parts = []
        for m in (Mode.FLIGHT, Mode.RAIL, Mode.ROAD, Mode.CYCLE, Mode.WATER, Mode.WALK):
            km = mk.get(int(m), 0.0)
            if km >= 1:
                parts.append(f"{tr(f'ui.mode.{m.name.lower()}')} {format_distance(km, unit, decimals=0)}")
        self.modes_lbl.setText(tr("ui.journey.by_mode") + " " + " · ".join(parts) if parts else "")
        # how many long trips each detection level finds, shown on its button
        flights = np.asarray(j.hop_mode) == Mode.FLIGHT
        self.trips.set_badges({k: str(sum(1 for leg in build_legs(j.cum_km, transfer_threshold_km(j.cum_km, k),
                                                                   flights) if leg[2])) for k in DETECTION})
        self.sketch.set_journey(j, self.state.project.journey.trip_detection)
        note = j.stats.get("note")
        self.banner.set("ok", tr("ui.journey.ready") + (f"\n{note}" if note else ""))
        self.state.journey_cache = j
        self._rec = rec
        self._show_pace()

    def _show_pace(self):
        rec = self._rec
        if not rec:
            return
        cur = self.state.project.video.duration_s
        calm, lively = rec["comfortable_s"], rec["brisk_s"]
        if cur < lively * 0.8:
            kind, verdict = "warn", tr("ui.journey.pace_fast")
        elif cur < calm * 0.8:
            kind, verdict = "info", tr("ui.journey.pace_lively")
        else:
            kind, verdict = "ok", tr("ui.journey.pace_calm")
        self.pace.set(kind, tr("ui.journey.pace", cur=f"{cur:.0f}", verdict=verdict, calm=f"{calm:.0f}",
                               lively=f"{lively:.0f}"))
        self.use_calm.setText(tr("ui.journey.use_calm", s=f"{calm:.0f}"))
        self.use_lively.setText(tr("ui.journey.use_lively", s=f"{lively:.0f}"))
        for b in (self.use_calm, self.use_lively):
            b.setEnabled(True)
