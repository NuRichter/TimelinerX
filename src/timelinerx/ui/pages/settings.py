from __future__ import annotations

import json
import shutil
import urllib.request
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QLineEdit, QMessageBox, QPushButton, QSpinBox)

from ... import __version__
from ...encoding import ffmpeg as ff
from ...i18n import LANGUAGES, tr
from ...maps.keys import KEY_HELP_URL, key_source, mask, resolve_key, set_user_key, verify_key
from ...plugins.loader import load_plugins
from ...utils.paths import cache_dir, data_dir, jobs_dir, logs_dir, plugins_dir
from ..widgets import Banner, Card, ScrollPage, muted, page_header, primary_button
from ..workers import Worker


class SettingsPage(ScrollPage):
    def __init__(self, state, on_ui_changed):
        super().__init__()
        self.state = state
        self.on_ui_changed = on_ui_changed
        s = state.settings
        self.lay.addWidget(page_header(tr("ui.settings.title"), tr("ui.settings.subtitle")))
        self.banner = Banner("info", "")
        self.banner.hide()
        self.lay.addWidget(self.banner)

        r = Card(tr("ui.settings.rendering"))
        f = QFormLayout()
        self.ffpath = QLineEdit(s.rendering.ffmpeg_path)
        self.ffpath.setPlaceholderText(tr("ui.settings.ffmpeg_auto"))
        fb = QPushButton(tr("ui.browse"))
        fb.clicked.connect(self.pick_ffmpeg)
        ft = QPushButton(tr("ui.settings.test"))
        ft.clicked.connect(self.test_ffmpeg)
        row = QHBoxLayout()
        row.addWidget(self.ffpath)
        row.addWidget(fb)
        row.addWidget(ft)
        f.addRow(tr("ui.settings.ffmpeg"), row)
        self.def_enc = QComboBox()
        for k in ("auto", "nvenc", "qsv", "amf", "software"):
            self.def_enc.addItem(tr(f"ui.video.encoder.{k}"), k)
        self.def_enc.setCurrentIndex(self.def_enc.findData(s.rendering.default_encoder))
        self.def_q = QComboBox()
        for q in ("draft", "standard", "high", "cinematic"):
            self.def_q.addItem(tr(f"ui.video.quality.{q}"), q)
        self.def_q.setCurrentIndex(self.def_q.findData(s.rendering.default_quality))
        self.seg = QDoubleSpinBox()
        self.seg.setRange(1, 30)
        self.seg.setSuffix(" s")
        self.seg.setValue(s.rendering.segment_seconds)
        self.keep = QCheckBox(tr("ui.settings.keep_jobs"))
        self.keep.setChecked(s.rendering.keep_job_files)
        self.auto_fb = QCheckBox(tr("ui.settings.auto_fallback"))
        self.auto_fb.setChecked(s.rendering.accept_encoder_fallback_automatically)
        f.addRow(tr("ui.settings.default_encoder"), self.def_enc)
        f.addRow(tr("ui.settings.default_quality"), self.def_q)
        f.addRow(tr("ui.settings.segment"), self.seg)
        f.addRow("", muted(tr("ui.settings.segment_note")))
        f.addRow("", self.keep)
        f.addRow("", self.auto_fb)
        f.addRow("", muted(tr("ui.settings.auto_fallback_note")))
        r.add(f)
        self.lay.addWidget(r)

        m = Card(tr("ui.settings.maps"))
        mf = QFormLayout()
        self.def_prov = QComboBox()
        for k in ("carto", "carto-voyager", "plain"):
            self.def_prov.addItem(k, k)
        self.def_prov.setCurrentIndex(max(0, self.def_prov.findData(s.maps.default_provider)))
        self.offline = QCheckBox(tr("ui.visual.offline"))
        self.offline.setChecked(s.maps.offline_mode)
        self.cache_lbl = muted("")
        cc = QPushButton(tr("ui.settings.clear_cache"))
        cc.clicked.connect(self.clear_cache)
        mf.addRow(tr("ui.settings.default_provider"), self.def_prov)
        self.carto_key = QLineEdit(s.maps.carto_api_key)
        self.carto_key.setEchoMode(QLineEdit.Password)
        self.carto_key.setPlaceholderText(tr("ui.settings.carto_key_placeholder"))
        self.carto_key.setAccessibleName(tr("ui.settings.carto_key"))
        self.carto_key.textChanged.connect(lambda _t: self._key_status())
        show = QPushButton(tr("ui.settings.carto_key_show"))
        show.setCheckable(True)
        show.toggled.connect(lambda on: self.carto_key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        verify = QPushButton(tr("ui.settings.carto_key_verify"))
        verify.clicked.connect(self.verify_carto_key)
        krow = QHBoxLayout()
        krow.addWidget(self.carto_key, 1)
        krow.addWidget(show)
        krow.addWidget(verify)
        mf.addRow(tr("ui.settings.carto_key"), krow)
        self.key_status = muted("")
        self.key_status.setWordWrap(True)
        mf.addRow("", self.key_status)
        how = muted(tr("ui.settings.carto_key_note", url=KEY_HELP_URL))
        how.setOpenExternalLinks(True)
        how.setTextFormat(Qt.RichText)
        how.setWordWrap(True)
        mf.addRow("", how)
        mf.addRow("", self.offline)
        mf.addRow(tr("ui.settings.tile_cache"), self.cache_lbl)
        mf.addRow("", cc)
        mf.addRow("", muted(tr("ui.settings.tiles_policy")))
        m.add(mf)
        self.lay.addWidget(m)

        p = Card(tr("ui.settings.privacy"))
        self.debug_coords = QCheckBox(tr("ui.settings.debug_coords"))
        self.debug_coords.setChecked(s.privacy.debug_log_coordinates)
        p.add(self.debug_coords)
        p.add(muted(tr("ui.settings.privacy_note")))
        self.lay.addWidget(p)

        pf = Card(tr("ui.settings.performance"))
        pff = QFormLayout()
        self.lru = QSpinBox()
        self.lru.setRange(64, 8192)
        self.lru.setValue(s.performance.tile_memory_cache)
        pff.addRow(tr("ui.settings.tile_mem"), self.lru)
        pf.add(pff)
        self.lay.addWidget(pf)

        u = Card(tr("ui.settings.ui"))
        uf = QFormLayout()
        self.lang = QComboBox()
        for k, v in LANGUAGES.items():
            self.lang.addItem(v, k)
        self.lang.setCurrentIndex(max(0, self.lang.findData(s.ui.language)))
        self.theme = QComboBox()
        for k in ("system", "light", "dark"):
            self.theme.addItem(tr(f"ui.settings.theme.{k}"), k)
        self.theme.setCurrentIndex(max(0, self.theme.findData(s.ui.theme)))
        self.motion = QComboBox()
        for k in ("system", "on", "off"):
            self.motion.addItem(tr(f"ui.settings.motion.{k}"), k)
        self.motion.setCurrentIndex(max(0, self.motion.findData(s.ui.reduced_motion)))
        self.adv = QCheckBox(tr("ui.settings.advanced"))
        self.adv.setChecked(s.ui.advanced_diagnostics)
        self.unit = QComboBox()
        self.unit.addItem(tr("ui.settings.unit.km"), "km")
        self.unit.addItem(tr("ui.settings.unit.mi"), "mi")
        self.unit.setCurrentIndex(max(0, self.unit.findData(s.ui.distance_unit)))
        self.unit.setAccessibleName(tr("ui.settings.distance_unit"))
        uf.addRow(tr("ui.settings.language"), self.lang)
        uf.addRow(tr("ui.settings.distance_unit"), self.unit)
        uf.addRow(tr("ui.settings.appearance"), self.theme)
        uf.addRow(tr("ui.settings.reduced_motion"), self.motion)
        uf.addRow("", self.adv)
        uf.addRow("", muted(tr("ui.settings.unit_note")))
        uf.addRow("", muted(tr("ui.settings.restart_note")))
        u.add(uf)
        self.lay.addWidget(u)

        st = Card(tr("ui.settings.storage"))
        sf = QFormLayout()
        self.outdir = QLineEdit(s.storage.output_dir)
        ob = QPushButton(tr("ui.browse"))
        ob.clicked.connect(lambda: self._pick_dir(self.outdir))
        orow = QHBoxLayout()
        orow.addWidget(self.outdir)
        orow.addWidget(ob)
        self.watch = QLineEdit(s.storage.watch_folder)
        wb = QPushButton(tr("ui.browse"))
        wb.clicked.connect(lambda: self._pick_dir(self.watch))
        wrow = QHBoxLayout()
        wrow.addWidget(self.watch)
        wrow.addWidget(wb)
        self.watch_on = QCheckBox(tr("ui.settings.watch_enable"))
        self.watch_on.setChecked(s.storage.watch_enabled)
        self.watch_preset = QLineEdit(s.storage.watch_preset_project)
        pb = QPushButton(tr("ui.browse"))
        pb.clicked.connect(self._pick_preset)
        prow = QHBoxLayout()
        prow.addWidget(self.watch_preset)
        prow.addWidget(pb)
        sf.addRow(tr("ui.settings.output_dir"), orow)
        sf.addRow(tr("ui.settings.watch_folder"), wrow)
        sf.addRow(tr("ui.settings.watch_preset"), prow)
        sf.addRow("", self.watch_on)
        sf.addRow("", muted(tr("ui.settings.watch_note")))
        locs = QPushButton(tr("ui.settings.open_data"))
        locs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(data_dir()))))
        logs = QPushButton(tr("ui.settings.open_logs"))
        logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(logs_dir()))))
        lrow = QHBoxLayout()
        lrow.addWidget(locs)
        lrow.addWidget(logs)
        lrow.addStretch(1)
        sf.addRow("", lrow)
        st.add(sf)
        self.lay.addWidget(st)

        pl = Card(tr("ui.settings.plugins"))
        self.py_plugins = QCheckBox(tr("ui.settings.python_plugins"))
        self.py_plugins.setChecked(bool(getattr(s.storage, "load_python_plugins", False)))
        pl.add(self.py_plugins)
        pl.add(muted(tr("ui.settings.plugins_note", path=str(plugins_dir()))))
        self.plugin_lbl = muted("")
        pl.add(self.plugin_lbl)
        prow2 = QHBoxLayout()
        b1 = QPushButton(tr("ui.settings.reload_plugins"))
        b1.clicked.connect(self.reload_plugins)
        b2 = QPushButton(tr("ui.settings.open_plugins"))
        b2.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(plugins_dir()))))
        prow2.addWidget(b1)
        prow2.addWidget(b2)
        prow2.addStretch(1)
        pl.add(prow2)
        self.lay.addWidget(pl)

        up = Card(tr("ui.settings.updates"))
        upf = QFormLayout()
        self.manifest = QLineEdit(s.storage.update_manifest_url)
        self.manifest.setPlaceholderText("https://…/timelinerx.json")
        chk = QPushButton(tr("ui.settings.check_now"))
        chk.clicked.connect(self.check_updates)
        upf.addRow(tr("ui.settings.manifest"), self.manifest)
        upf.addRow("", chk)
        upf.addRow("", muted(tr("ui.settings.updates_note", version=__version__)))
        up.add(upf)
        self.lay.addWidget(up)

        save = primary_button(tr("ui.settings.save"))
        save.clicked.connect(self.save)
        r2 = QHBoxLayout()
        r2.addWidget(save)
        r2.addStretch(1)
        self.lay.addLayout(r2)
        self.finish()
        self._key_status()
        self.update_cache_label()
        self.reload_plugins()

    def _pick_dir(self, edit):
        d = QFileDialog.getExistingDirectory(self, "", edit.text() or str(Path.home()))
        if d:
            edit.setText(d)

    def _pick_preset(self):
        f, _ = QFileDialog.getOpenFileName(self, "", "", "TimelinerX project (*.nrproj)")
        if f:
            self.watch_preset.setText(f)

    def pick_ffmpeg(self):
        f, _ = QFileDialog.getOpenFileName(self, "FFmpeg", "", "ffmpeg (ffmpeg*);;All files (*)")
        if f:
            self.ffpath.setText(f)

    def test_ffmpeg(self):
        path = self.ffpath.text().strip() or None
        self.banner.set("info", tr("ui.settings.testing"))
        self.banner.show()
        w = Worker(lambda _w: ff.probe(path, force=True), self)
        w.done.connect(lambda info: self.banner.set("ok", tr("ui.settings.ffmpeg_ok", version=info.version,
                                                             enc=", ".join(k for k, v in info.hw_verified.items()
                                                                           if v) or "software only")))
        w.failed.connect(lambda e: self.banner.set("danger", str(e)))
        w.start()
        self._w = w

    def _key_status(self):
        typed = self.carto_key.text().strip()
        src = "settings" if typed else key_source()
        if src == "none":
            self.key_status.setText(tr("ui.settings.carto_key_none"))
        elif src == "settings":
            self.key_status.setText(tr("ui.settings.carto_key_from_settings", key=mask(typed)))
        elif src == "environment":
            self.key_status.setText(tr("ui.settings.carto_key_from_env"))
        else:
            self.key_status.setText(tr("ui.settings.carto_key_from_build"))

    def verify_carto_key(self):
        key = self.carto_key.text().strip() or resolve_key()
        self.banner.show()
        if not key:
            self.banner.set("warn", tr("ui.settings.carto_key_none"))
            return
        self.banner.set("info", tr("ui.settings.carto_key_checking"))
        w = Worker(lambda _w: verify_key(key), self)
        w.done.connect(lambda r: self.banner.set("ok" if r.ok else "warn",
                                                 tr("ui.settings.carto_key_ok") if r.ok else
                                                 tr("ui.settings.carto_key_bad", msg=r.message
                                                    + (f" ({r.detail})" if r.detail else ""))))
        w.failed.connect(lambda e: self.banner.set("danger", str(e)))
        w.start()
        self._kw = w

    def update_cache_label(self):
        root = cache_dir()
        total = sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) if root.exists() else 0
        jobs = sum(p.stat().st_size for p in jobs_dir().rglob("*") if p.is_file())
        self.cache_lbl.setText(tr("ui.settings.cache_size", mb=f"{total / 2 ** 20:,.1f}", jobs=f"{jobs / 2 ** 20:,.1f}"))

    def clear_cache(self):
        if QMessageBox.question(self, tr("ui.settings.clear_cache"), tr("ui.settings.clear_confirm")) != QMessageBox.Yes:
            return
        for sub in ("tiles", "graded", "import"):
            shutil.rmtree(cache_dir() / sub, ignore_errors=True)
        self.update_cache_label()

    def reload_plugins(self):
        rep = load_plugins(load_python=self.py_plugins.isChecked())
        lines = [tr("ui.settings.plugin_themes", n=len(rep.themes)) + (": " + ", ".join(rep.themes) if rep.themes else ""),
                 tr("ui.settings.plugin_providers", n=len(rep.providers))
                 + (": " + ", ".join(rep.providers) if rep.providers else "")]
        lines += ["⚠ " + e for e in rep.errors]
        self.plugin_lbl.setText("\n".join(lines))

    def check_updates(self):
        url = self.manifest.text().strip()
        self.banner.show()
        if not url.startswith("https://"):
            self.banner.set("info", tr("ui.settings.no_manifest"))
            return

        def job(_w):
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "TimelinerX"}),
                                        timeout=10) as r:
                return json.loads(r.read(65536).decode())

        w = Worker(job, self)
        w.done.connect(lambda d: self.banner.set(
            "ok" if str(d.get("version")) == __version__ else "info",
            tr("ui.settings.update_result", latest=d.get("version", "?"), current=__version__,
               url=d.get("url", ""))))
        w.failed.connect(lambda e: self.banner.set("warn", tr("ui.settings.update_failed") + f" {e}"))
        w.start()
        self._uw = w

    def save(self):
        s = self.state.settings
        s.rendering.ffmpeg_path = self.ffpath.text().strip()
        s.rendering.default_encoder = self.def_enc.currentData()
        s.rendering.default_quality = self.def_q.currentData()
        s.rendering.segment_seconds = self.seg.value()
        s.rendering.keep_job_files = self.keep.isChecked()
        s.rendering.accept_encoder_fallback_automatically = self.auto_fb.isChecked()
        s.maps.default_provider = self.def_prov.currentData()
        s.maps.offline_mode = self.offline.isChecked()
        s.maps.carto_api_key = self.carto_key.text().strip()
        set_user_key(s.maps.carto_api_key)
        s.privacy.debug_log_coordinates = self.debug_coords.isChecked()
        s.performance.tile_memory_cache = self.lru.value()
        lang_changed = s.ui.language != self.lang.currentData()
        s.ui.language = self.lang.currentData()
        s.ui.distance_unit = self.unit.currentData()
        self.state.project.title.unit = s.ui.distance_unit
        s.ui.theme = self.theme.currentData()
        s.ui.reduced_motion = self.motion.currentData()
        s.ui.advanced_diagnostics = self.adv.isChecked()
        s.storage.output_dir = self.outdir.text().strip()
        s.storage.watch_folder = self.watch.text().strip()
        s.storage.watch_preset_project = self.watch_preset.text().strip()
        s.storage.watch_enabled = self.watch_on.isChecked()
        s.storage.update_manifest_url = self.manifest.text().strip()
        setattr(s.storage, "load_python_plugins", self.py_plugins.isChecked())
        s.save()
        self.banner.set("ok", tr("ui.settings.saved"))
        self.banner.show()
        self.state.settingsChanged.emit()
        self.on_ui_changed()
        if lang_changed:
            self._offer_restart()

    def _offer_restart(self):
        """The interface language is applied at start-up; offer to restart right away."""
        import sys
        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QApplication
        box = QMessageBox(self)
        box.setWindowTitle(tr("ui.settings.language"))
        box.setText(tr("ui.settings.restart_now_q", lang=LANGUAGES.get(self.lang.currentData(), "")))
        now = box.addButton(tr("ui.settings.restart_now"), QMessageBox.AcceptRole)
        box.addButton(tr("ui.settings.restart_later"), QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not now:
            return
        win = self.window()
        if hasattr(win, "pages") and win.pages["queue"].busy():
            QMessageBox.information(self, tr("ui.settings.language"), tr("ui.settings.restart_busy"))
            return
        args = sys.argv[1:] if getattr(sys, "frozen", False) else ["-m", "timelinerx.app.main", *sys.argv[1:]]
        QProcess.startDetached(sys.executable, args)
        QApplication.instance().quit()
