from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QLineEdit, QPushButton, QSpinBox)

from ...core.errors import UsageError
from ...encoding.ffmpeg import BITS_PER_PIXEL
from ...i18n import tr
from ...pipeline.render_job import default_output_path
from ...projects.project import FPS_CHOICES, QUALITY, RESOLUTIONS
from ..widgets import Banner, Card, ScrollPage, muted, page_header, primary_button


class VideoPage(ScrollPage):
    def __init__(self, state, enqueue):
        super().__init__()
        self.state = state
        self._loading = False
        self.lay.addWidget(page_header(tr("ui.video.title"), tr("ui.video.subtitle")))
        c = Card(tr("ui.video.format"))
        f = QFormLayout()
        self.res = QComboBox()
        for r in RESOLUTIONS:
            self.res.addItem(r, r)
        self.aspect = QComboBox()
        for k in ("16:9", "9:16", "1:1", "custom"):
            self.aspect.addItem(tr(f"ui.video.aspect.{k.replace(':', 'x')}"), k)
        self.cw = QSpinBox()
        self.cw.setRange(128, 8192)
        self.cw.setSingleStep(2)
        self.ch = QSpinBox()
        self.ch.setRange(128, 8192)
        self.ch.setSingleStep(2)
        crow = QHBoxLayout()
        crow.addWidget(self.cw)
        crow.addWidget(self.ch)
        self.fps = QComboBox()
        for v in FPS_CHOICES:
            self.fps.addItem(f"{v} fps", v)
        self.dur = QDoubleSpinBox()
        self.dur.setRange(5, 1800)
        self.dur.setSuffix(" s")
        self.dur.setDecimals(1)
        self.quality = QComboBox()
        for q in QUALITY:
            self.quality.addItem(tr(f"ui.video.quality.{q}"), q)
        self.codec = QComboBox()
        self.codec.addItem("H.264", "h264")
        self.codec.addItem("HEVC (H.265)", "hevc")
        self.encoder = QComboBox()
        for k in ("auto", "nvenc", "qsv", "amf", "software"):
            self.encoder.addItem(tr(f"ui.video.encoder.{k}"), k)
        self.two_pass = QCheckBox(tr("ui.video.two_pass"))
        self.hdr = QCheckBox(tr("ui.video.hdr"))
        self.blur = QCheckBox(tr("ui.video.motion_blur"))
        f.addRow(tr("ui.video.resolution"), self.res)
        f.addRow(tr("ui.video.aspect"), self.aspect)
        f.addRow(tr("ui.video.custom"), crow)
        f.addRow(tr("ui.video.fps"), self.fps)
        f.addRow(tr("ui.video.duration"), self.dur)
        self.dur_hint = muted(tr("ui.video.duration_hint"))
        f.addRow("", self.dur_hint)
        f.addRow(tr("ui.video.quality"), self.quality)
        f.addRow(tr("ui.video.codec"), self.codec)
        f.addRow(tr("ui.video.encoder"), self.encoder)
        f.addRow("", self.two_pass)
        f.addRow("", muted(tr("ui.video.two_pass_note")))
        f.addRow("", self.blur)
        f.addRow("", muted(tr("ui.video.motion_blur_note")))
        f.addRow("", self.hdr)
        f.addRow("", muted(tr("ui.video.hdr_note")))
        c.add(f)
        self.dims = muted("")
        c.add(self.dims)
        self.lay.addWidget(c)
        self.warn = Banner("warn", "")
        self.warn.hide()
        self.lay.addWidget(self.warn)

        a = Card(tr("ui.audio.title"))
        af = QFormLayout()
        self.a_on = QCheckBox(tr("ui.audio.enable"))
        self.a_path = QLineEdit()
        ab = QPushButton(tr("ui.browse"))
        ab.clicked.connect(self._pick_audio)
        arow = QHBoxLayout()
        arow.addWidget(self.a_path)
        arow.addWidget(ab)
        self.a_vol = QDoubleSpinBox()
        self.a_vol.setRange(0, 2)
        self.a_vol.setSingleStep(0.05)
        self.a_beat = QCheckBox(tr("ui.audio.beat_sync"))
        self.a_duck = QCheckBox(tr("ui.audio.ducking"))
        self.a_fade = QDoubleSpinBox()
        self.a_fade.setRange(0, 10)
        self.a_fade.setSuffix(" s")
        af.addRow("", self.a_on)
        af.addRow(tr("ui.audio.file"), arow)
        af.addRow(tr("ui.audio.volume"), self.a_vol)
        af.addRow("", self.a_beat)
        af.addRow("", self.a_duck)
        af.addRow(tr("ui.audio.fade"), self.a_fade)
        a.add(af)
        a.add(muted(tr("ui.audio.note")))
        self.lay.addWidget(a)

        o = Card(tr("ui.video.output"))
        of = QHBoxLayout()
        self.out = QLineEdit()
        self.out.setAccessibleName(tr("ui.video.output"))
        ob = QPushButton(tr("ui.browse"))
        ob.clicked.connect(self._pick_out)
        of.addWidget(self.out)
        of.addWidget(ob)
        o.add(of)
        self.lay.addWidget(o)
        r = QHBoxLayout()
        pv = QPushButton(tr("ui.video.preview"))
        pv.clicked.connect(lambda: state.navigate.emit("preview"))
        go = primary_button(tr("ui.video.render"))
        go.clicked.connect(lambda: enqueue(self.out.text().strip()))
        r.addWidget(pv)
        r.addWidget(go)
        r.addStretch(1)
        self.lay.addLayout(r)
        self.finish()
        for w in (self.res, self.aspect, self.fps, self.quality, self.codec, self.encoder):
            w.currentIndexChanged.connect(self.save)
        for w in (self.cw, self.ch):
            w.valueChanged.connect(self.save)
        self.dur.valueChanged.connect(self.save)
        for w in (self.two_pass, self.hdr, self.blur, self.a_on, self.a_beat, self.a_duck):
            w.toggled.connect(self.save)
        for w in (self.a_vol, self.a_fade):
            w.valueChanged.connect(self.save)
        self.a_path.editingFinished.connect(self.save)
        state.projectChanged.connect(self.load)
        state.envChanged.connect(self.update_warnings)
        self.load()

    def _pick_audio(self):
        f, _ = QFileDialog.getOpenFileName(self, tr("ui.audio.file"), "", "Audio (*.mp3 *.wav *.flac *.m4a *.ogg)")
        if f:
            self.a_path.setText(f)
            self.a_on.setChecked(True)
            self.save()

    def _pick_out(self):
        f, _ = QFileDialog.getSaveFileName(self, tr("ui.video.output"), self.out.text(), "MP4 (*.mp4)")
        if f:
            self.out.setText(f if f.lower().endswith(".mp4") else f + ".mp4")

    def load(self):
        self._loading = True
        v = self.state.project.video
        self.res.setCurrentIndex(max(0, self.res.findData(v.resolution)))
        self.aspect.setCurrentIndex(max(0, self.aspect.findData(v.aspect)))
        self.cw.setValue(v.width or 1920)
        self.ch.setValue(v.height or 1080)
        self.cw.setEnabled(v.aspect == "custom")
        self.ch.setEnabled(v.aspect == "custom")
        self.fps.setCurrentIndex(max(0, self.fps.findData(v.fps)))
        self.dur.setValue(v.duration_s)
        self.quality.setCurrentIndex(max(0, self.quality.findData(v.quality)))
        self.codec.setCurrentIndex(max(0, self.codec.findData(v.codec)))
        self.encoder.setCurrentIndex(max(0, self.encoder.findData(v.encoder)))
        self.two_pass.setChecked(v.two_pass)
        self.blur.setChecked(v.motion_blur)
        self.hdr.setChecked(v.hdr)
        au = self.state.project.audio
        self.a_on.setChecked(au.enabled)
        self.a_path.setText(au.path or "")
        self.a_vol.setValue(au.volume)
        self.a_beat.setChecked(au.beat_sync)
        self.a_duck.setChecked(au.ducking)
        self.a_fade.setValue(au.fade_out_s)
        if not self.out.text():
            out_dir = self.state.settings.storage.output_dir or None
            try:
                self.out.setText(str(default_output_path(self.state.project, out_dir)))
            except UsageError:
                pass
        self._loading = False
        self.update_warnings()

    def save(self, *_):
        if self._loading:
            return
        v = self.state.project.video
        v.resolution = self.res.currentData()
        v.aspect = self.aspect.currentData()
        custom = v.aspect == "custom"
        self.cw.setEnabled(custom)
        self.ch.setEnabled(custom)
        v.width, v.height = (self.cw.value(), self.ch.value()) if custom else (None, None)
        v.fps = self.fps.currentData()
        v.duration_s = self.dur.value()
        v.quality = self.quality.currentData()
        v.codec = self.codec.currentData()
        v.encoder = self.encoder.currentData()
        v.two_pass = self.two_pass.isChecked()
        v.motion_blur = self.blur.isChecked()
        v.hdr = self.hdr.isChecked()
        if v.hdr and v.codec != "hevc":
            self.codec.setCurrentIndex(self.codec.findData("hevc"))
            v.codec = "hevc"
        au = self.state.project.audio
        au.enabled, au.path = self.a_on.isChecked(), self.a_path.text().strip() or None
        au.volume, au.beat_sync, au.ducking = self.a_vol.value(), self.a_beat.isChecked(), self.a_duck.isChecked()
        au.fade_out_s = self.a_fade.value()
        try:
            self.out.setText(str(default_output_path(self.state.project, str(Path(self.out.text()).parent)
                                                     if self.out.text() else None)))
        except UsageError:
            pass
        self.update_warnings()
        self.state.touch_project()

    def update_warnings(self):
        p = self.state.project
        errs = p.validate()
        try:
            w, h = p.video.dimensions()
            self.dims.setText(tr("ui.video.dims", w=w, h=h, frames=int(p.video.duration_s * p.video.fps)))
        except UsageError as e:
            self.dims.setText(str(e))
            w = h = 0
        msgs = [e for e in errs if "Timeline" not in e]
        if min(w, h) >= 1440 or max(w, h) >= 3840:
            env = self.state.env_report
            bpp = BITS_PER_PIXEL[p.video.quality]
            est_mb = bpp * w * h * p.video.fps * p.video.duration_s / 8 / 2 ** 20
            msgs.append(tr("ui.video.hires_warn", mb=f"{est_mb * 2.2:,.0f}"))
            if env is not None:
                if (env.ram_total_gb or 0) < 16:
                    msgs.append(tr("ui.video.hires_ram", gb=env.ram_total_gb))
                vram = max([g.vram_mb or 0 for g in env.gpus] or [0])
                if vram and vram < 4096:
                    msgs.append(tr("ui.video.hires_vram", mb=vram))
                hw = [e for e, ok in ((env.ffmpeg or {}).get("hw_verified") or {}).items() if ok]
                if not hw:
                    msgs.append(tr("ui.video.hires_nohw"))
            try:
                free = shutil.disk_usage(Path(self.out.text()).parent if self.out.text() else Path.home()).free
                if free < est_mb * 2.2 * 2 ** 20:
                    msgs.append(tr("ui.video.hires_disk", gb=f"{free / 2 ** 30:.1f}"))
            except OSError:
                pass
            msgs.append(tr("ui.video.hires_continue"))
        if msgs:
            self.warn.set("warn", "\n".join(msgs))
            self.warn.show()
        else:
            self.warn.hide()
