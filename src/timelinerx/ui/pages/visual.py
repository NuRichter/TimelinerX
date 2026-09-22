from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
                               QTableWidget, QTableWidgetItem, QTabWidget, QWidget)

from ...camera.planner import CAMERA_MODES
from ...i18n import LANGUAGES, tr
from ...rendering.styles import load_themes
from ..widgets import Card, ScrollPage, ThemeSwatch, muted, page_header, primary_button


def _combo(items, current=None, label=""):
    c = QComboBox()
    for key, text in items:
        c.addItem(text, key)
    if current is not None:
        c.setCurrentIndex(max(0, c.findData(current)))
    c.setAccessibleName(label)
    return c


def _dspin(lo, hi, step, val, suffix=""):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setDecimals(2)
    s.setValue(val)
    if suffix:
        s.setSuffix(suffix)
    return s


class VisualPage(ScrollPage):
    def __init__(self, state):
        super().__init__()
        self.state = state
        self._loading = False
        self.lay.addWidget(page_header(tr("ui.visual.title"), tr("ui.visual.subtitle")))
        tabs = QTabWidget()
        tabs.addTab(self._style_tab(), tr("ui.visual.tab.style"))
        tabs.addTab(self._camera_tab(), tr("ui.visual.tab.camera"))
        tabs.addTab(self._title_tab(), tr("ui.visual.tab.titles"))
        self.lay.addWidget(tabs)
        nb = primary_button(tr("ui.visual.next"))
        nb.clicked.connect(lambda: state.navigate.emit("video"))
        r = QHBoxLayout()
        r.addWidget(nb)
        r.addStretch(1)
        self.lay.addLayout(r)
        self.finish()
        state.projectChanged.connect(self.load)
        self.load()

    # ------------------------------------------------------------------ tabs
    def _style_tab(self):
        w = QWidget()
        lay = QGridLayout(w)
        lay.setContentsMargins(16, 16, 16, 16)
        self.swatches = QButtonGroup(self)
        self.swatches.setExclusive(True)
        grid = QGridLayout()
        for i, t in enumerate(load_themes().values()):
            sw = ThemeSwatch(t)
            self.swatches.addButton(sw)
            grid.addWidget(sw, i // 4, i % 4)
        self.swatches.buttonClicked.connect(self.save)
        lay.addLayout(grid, 0, 0, 1, 2)
        f = QFormLayout()
        self.provider = _combo([("carto", tr("ui.visual.provider.carto")),
                                ("carto-voyager", "CARTO Voyager"), ("plain", tr("ui.visual.provider.plain")),
                                ("mbtiles", tr("ui.visual.provider.mbtiles"))], label=tr("ui.visual.provider"))
        self.mbtiles = QLineEdit()
        self.mbtiles.setPlaceholderText("…/map.mbtiles")
        mb = QPushButton(tr("ui.browse"))
        mb.clicked.connect(self._pick_mbtiles)
        mrow = QHBoxLayout()
        mrow.addWidget(self.mbtiles)
        mrow.addWidget(mb)
        self.offline = QCheckBox(tr("ui.visual.offline"))
        f.addRow(tr("ui.visual.provider"), self.provider)
        f.addRow(tr("ui.visual.mbtiles"), mrow)
        f.addRow("", self.offline)
        f.addRow("", muted(tr("ui.visual.provider_note")))
        self.trail_len = _dspin(0.1, 4.0, 0.1, 1.0, "×")
        self.trail_w = _dspin(0.3, 3.0, 0.1, 1.0, "×")
        self.gradient = QCheckBox(tr("ui.visual.gradient"))
        self.glow = QCheckBox(tr("ui.visual.glow"))
        self.shadow = QCheckBox(tr("ui.visual.shadow"))
        self.pulse = QCheckBox(tr("ui.visual.pulse"))
        self.full_route = QCheckBox(tr("ui.visual.full_route"))
        self.vignette = QCheckBox(tr("ui.visual.vignette"))
        self.grain = QCheckBox(tr("ui.visual.grain"))
        f.addRow(tr("ui.visual.trail_length"), self.trail_len)
        f.addRow(tr("ui.visual.trail_width"), self.trail_w)
        for cb in (self.gradient, self.glow, self.shadow, self.pulse, self.full_route, self.vignette, self.grain):
            f.addRow("", cb)
        lay.addLayout(f, 1, 0, 1, 2)
        for wdg in (self.provider,):
            wdg.currentIndexChanged.connect(self.save)
        self.mbtiles.editingFinished.connect(self.save)
        for wdg in (self.trail_len, self.trail_w):
            wdg.valueChanged.connect(self.save)
        for cb in (self.offline, self.gradient, self.glow, self.shadow, self.pulse, self.full_route,
                   self.vignette, self.grain):
            cb.toggled.connect(self.save)
        return w

    def _pick_mbtiles(self):
        f, _ = QFileDialog.getOpenFileName(self, "MBTiles", "", "MBTiles (*.mbtiles)")
        if f:
            self.mbtiles.setText(f)
            self.provider.setCurrentIndex(self.provider.findData("mbtiles"))
            self.save()

    def _camera_tab(self):
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 16, 16, 16)
        f = QFormLayout()
        moved = QHBoxLayout()
        moved.addWidget(muted(tr("ui.camera.moved_note")), 1)
        go = QPushButton(tr("ui.camera.open_journey"))
        go.clicked.connect(lambda: self.state.navigate.emit("journey"))
        moved.addWidget(go)
        self.composition = _combo([("thirds", tr("ui.camera.thirds")), ("centered", tr("ui.camera.centered"))],
                                  label=tr("ui.camera.composition"))
        self.pacing = _combo([(k, tr(f"ui.camera.pacing.{k}")) for k in ("visual_zoom", "visual", "distance")],
                             label=tr("ui.camera.pacing"))
        self.smooth = _dspin(0.0, 3.0, 0.1, 1.0, "×")
        self.antic = _dspin(0.0, 3.0, 0.1, 1.0, "×")
        self.intro = _dspin(0.0, 6.0, 0.1, 1.2, " s")
        self.outro_t = _dspin(0.2, 6.0, 0.1, 1.2, " s")
        self.outro_h = _dspin(0.0, 10.0, 0.1, 1.2, " s")
        f.addRow(moved)
        f.addRow(tr("ui.camera.composition"), self.composition)
        f.addRow(tr("ui.camera.pacing"), self.pacing)
        f.addRow(tr("ui.camera.smoothing"), self.smooth)
        f.addRow(tr("ui.camera.anticipation"), self.antic)
        f.addRow(tr("ui.camera.intro"), self.intro)
        f.addRow(tr("ui.camera.outro_transition"), self.outro_t)
        f.addRow(tr("ui.camera.outro_hold"), self.outro_h)
        lay.addLayout(f, 1)
        kf = Card(tr("ui.camera.keyframes"))
        kf.add(muted(tr("ui.camera.keyframes_note")))
        self.kf_table = QTableWidget(0, 6)
        self.kf_table.setHorizontalHeaderLabels(["t (s)", "lat", "lon", "span km", "hold s", "ramp s"])
        self.kf_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.kf_table.setMinimumHeight(180)
        self.kf_table.setAccessibleName(tr("ui.camera.keyframes"))
        kf.add(self.kf_table)
        kr = QHBoxLayout()
        add = QPushButton(tr("ui.camera.kf_add"))
        add.clicked.connect(self._kf_add)
        rem = QPushButton(tr("ui.camera.kf_remove"))
        rem.clicked.connect(self._kf_remove)
        kr.addWidget(add)
        kr.addWidget(rem)
        kr.addStretch(1)
        kf.add(kr)
        lay.addWidget(kf, 1)
        for c in (self.composition, self.pacing):
            c.currentIndexChanged.connect(self.save)
        for s in (self.smooth, self.antic, self.intro, self.outro_t, self.outro_h):
            s.valueChanged.connect(self.save)
        self.kf_table.itemChanged.connect(self.save)
        return w

    def _kf_add(self):
        p = self.state.project
        lat = lon = 0.0
        j = getattr(self.state, "journey_cache", None)
        if j is not None:
            lat, lon = j.position(j.total_km / 2)
        p.camera.keyframes.append({"time_s": round(p.video.duration_s / 2, 2), "lat": round(lat, 5),
                                   "lon": round(lon, 5), "span_km": 25.0, "hold_s": 1.5, "ramp_s": 1.2})
        self.load()
        self.state.touch_project()

    def add_keyframe(self, time_s: float, lat: float, lon: float, span_km: float):
        self.state.project.camera.keyframes.append({"time_s": round(time_s, 2), "lat": round(lat, 5),
                                                    "lon": round(lon, 5), "span_km": round(span_km, 2),
                                                    "hold_s": 1.5, "ramp_s": 1.2})
        self.load()
        self.state.touch_project()

    def _kf_remove(self):
        r = self.kf_table.currentRow()
        if 0 <= r < len(self.state.project.camera.keyframes):
            del self.state.project.camera.keyframes[r]
            self.load()
            self.state.touch_project()

    def _title_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(16, 16, 16, 16)
        self.template = QLineEdit()
        self.tname = QLineEdit()
        self.layout_c = _combo([(k, tr(f"ui.titles.layout.{k}")) for k in
                                ("corner", "centered", "minimal", "lower_third", "none")], label=tr("ui.titles.layout"))
        self.show_date = QCheckBox(tr("ui.titles.show_date"))
        self.date_fmt = _combo([("month", tr("ui.titles.date.month")), ("day", tr("ui.titles.date.day"))])
        self.show_dist = QCheckBox(tr("ui.titles.show_distance"))
        self.ending = QCheckBox(tr("ui.titles.ending"))
        self.tbar = QCheckBox(tr("ui.titles.timeline_bar"))
        self.countup = QCheckBox(tr("ui.titles.count_up"))
        self.ending_tpl = QLineEdit()
        self.lang = _combo(list(LANGUAGES.items()))
        f.addRow(tr("ui.titles.template"), self.template)
        f.addRow("", muted(tr("ui.titles.placeholders")))
        f.addRow(tr("ui.titles.name"), self.tname)
        f.addRow(tr("ui.titles.layout"), self.layout_c)
        f.addRow("", self.show_date)
        f.addRow(tr("ui.titles.date_format"), self.date_fmt)
        f.addRow("", self.show_dist)
        f.addRow("", self.tbar)
        f.addRow("", self.ending)
        f.addRow("", self.countup)
        f.addRow(tr("ui.titles.ending_template"), self.ending_tpl)
        f.addRow(tr("ui.titles.language"), self.lang)
        f.addRow("", muted(tr("ui.titles.safe_area")))
        for e in (self.template, self.tname, self.ending_tpl):
            e.editingFinished.connect(self.save)
        for c in (self.layout_c, self.date_fmt, self.lang):
            c.currentIndexChanged.connect(self.save)
        for cb in (self.show_date, self.show_dist, self.ending, self.tbar, self.countup):
            cb.toggled.connect(self.save)
        return w

    # ------------------------------------------------------------ load/save
    def load(self):
        self._loading = True
        p = self.state.project
        for b in self.swatches.buttons():
            b.setChecked(b.theme.id == p.visual.theme)
        prov = p.visual.map_provider
        if prov.startswith("mbtiles:"):
            self.mbtiles.setText(prov[len("mbtiles:"):])
            prov = "mbtiles"
        self.provider.setCurrentIndex(max(0, self.provider.findData(prov)))
        self.offline.setChecked(p.visual.offline)
        t = p.visual.trail
        self.trail_len.setValue(t.length)
        self.trail_w.setValue(t.width)
        self.gradient.setChecked(t.gradient)
        self.glow.setChecked(t.glow)
        self.shadow.setChecked(t.shadow)
        self.pulse.setChecked(t.pulse)
        self.full_route.setChecked(t.show_full_route)
        self.vignette.setChecked(p.visual.vignette)
        self.grain.setChecked(p.visual.grain)
        c = p.camera
        self.composition.setCurrentIndex(max(0, self.composition.findData(c.composition)))
        self.pacing.setCurrentIndex(max(0, self.pacing.findData(c.pacing)))
        self.smooth.setValue(c.smoothing)
        self.antic.setValue(c.anticipation)
        self.intro.setValue(c.intro_s)
        self.outro_t.setValue(c.outro_transition_s)
        self.outro_h.setValue(c.outro_hold_s)
        self.kf_table.setRowCount(0)
        for k in c.keyframes:
            r = self.kf_table.rowCount()
            self.kf_table.insertRow(r)
            for col, key in enumerate(("time_s", "lat", "lon", "span_km", "hold_s", "ramp_s")):
                self.kf_table.setItem(r, col, QTableWidgetItem(str(k.get(key, ""))))
        ti = p.title
        self.template.setText(ti.template)
        self.tname.setText(ti.name)
        self.layout_c.setCurrentIndex(max(0, self.layout_c.findData(ti.layout)))
        self.show_date.setChecked(ti.show_date)
        self.date_fmt.setCurrentIndex(max(0, self.date_fmt.findData(ti.date_format)))
        self.show_dist.setChecked(ti.show_distance)
        self.ending.setChecked(ti.ending_title)
        self.tbar.setChecked(ti.timeline_bar)
        self.countup.setChecked(ti.count_up)
        self.ending_tpl.setText(ti.ending_template)
        self.lang.setCurrentIndex(max(0, self.lang.findData(ti.language)))
        self._loading = False

    def save(self, *_):
        if self._loading:
            return
        p = self.state.project
        b = self.swatches.checkedButton()
        if b is not None:
            p.visual.theme = b.theme.id
        prov = self.provider.currentData()
        if prov == "mbtiles":
            prov = "mbtiles:" + self.mbtiles.text().strip() if self.mbtiles.text().strip() else "plain"
        p.visual.map_provider = prov
        p.visual.offline = self.offline.isChecked()
        t = p.visual.trail
        t.length, t.width = self.trail_len.value(), self.trail_w.value()
        t.gradient, t.glow, t.shadow = self.gradient.isChecked(), self.glow.isChecked(), self.shadow.isChecked()
        t.pulse, t.show_full_route = self.pulse.isChecked(), self.full_route.isChecked()
        p.visual.vignette, p.visual.grain = self.vignette.isChecked(), self.grain.isChecked()
        c = p.camera
        c.composition = self.composition.currentData()
        c.pacing = self.pacing.currentData()
        c.smoothing, c.anticipation = self.smooth.value(), self.antic.value()
        c.intro_s, c.outro_transition_s, c.outro_hold_s = self.intro.value(), self.outro_t.value(), self.outro_h.value()
        kfs = []
        for r in range(self.kf_table.rowCount()):
            try:
                vals = [float(self.kf_table.item(r, col).text()) for col in range(6)]
            except (AttributeError, ValueError):
                continue
            kfs.append(dict(zip(("time_s", "lat", "lon", "span_km", "hold_s", "ramp_s"), vals)))
        c.keyframes = kfs
        ti = p.title
        ti.template, ti.name = self.template.text(), self.tname.text()
        ti.layout = self.layout_c.currentData()
        ti.show_date, ti.show_distance, ti.ending_title = (self.show_date.isChecked(), self.show_dist.isChecked(),
                                                           self.ending.isChecked())
        ti.date_format = self.date_fmt.currentData()
        ti.timeline_bar, ti.count_up = self.tbar.isChecked(), self.countup.isChecked()
        ti.ending_template = self.ending_tpl.text()
        ti.language = self.lang.currentData()
        self.state.touch_project()
