from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QFileDialog, QHBoxLayout, QHeaderView,
                               QLabel, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem)

from ...core.errors import TimelinerXError
from ...i18n import tr
from ...repair.engine import Confidence, analyze, apply_plan
from ...timeline.parser import load_timeline
from ...utils.paths import cache_dir
from ..widgets import Banner, Card, DropZone, ScrollPage, button_text, muted, page_header, primary_button
from ..workers import Worker


class ImportPage(ScrollPage):
    def __init__(self, state):
        super().__init__()
        self.state = state
        self.worker = None
        self.plan = None
        self.last_path = None
        self.lay.addWidget(page_header(tr("ui.import.title"), tr("ui.import.subtitle")))
        self.drop = DropZone(tr("ui.import.drop"))
        self.drop.fileDropped.connect(self.load)
        self.lay.addWidget(self.drop)
        row = QHBoxLayout()
        b = primary_button(tr("ui.import.choose"))
        b.clicked.connect(self.choose)
        row.addWidget(b)
        row.addStretch(1)
        self.lay.addLayout(row)
        self.lay.addWidget(muted(tr("ui.import.privacy")))
        from ..tutorial import TutorialCard
        self.lay.addWidget(TutorialCard(state))
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setAccessibleName(tr("ui.import.progress"))
        self.progress.hide()
        self.lay.addWidget(self.progress)
        self.banner = Banner("info", "")
        self.banner.hide()
        self.lay.addWidget(self.banner)

        self.diag = Card(tr("ui.import.diagnostics"))
        self.diag_text = QLabel()
        self.diag_text.setWordWrap(True)
        self.diag_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.diag.add(self.diag_text)
        drow = QHBoxLayout()
        self.btn_repair = QPushButton(button_text(tr("ui.import.run_repair")))
        self.btn_repair.clicked.connect(self.run_analysis)
        self.btn_continue = primary_button(tr("ui.import.continue"))
        self.btn_continue.clicked.connect(lambda: state.navigate.emit("analysis"))
        drow.addWidget(self.btn_repair)
        drow.addWidget(self.btn_continue)
        drow.addStretch(1)
        self.diag.add(drow)
        self.diag.hide()
        self.lay.addWidget(self.diag)

        self.repair = Card(tr("ui.repair.title"))
        self.repair.add(muted(tr("ui.repair.explain")))
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([tr("ui.repair.apply"), tr("ui.repair.confidence"),
                                              tr("ui.repair.pass"), tr("ui.repair.action"),
                                              tr("ui.repair.location")])
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setMinimumHeight(260)
        self.table.setAccessibleName(tr("ui.repair.title"))
        self.repair.add(self.table)
        rrow = QHBoxLayout()
        self.btn_apply = primary_button(tr("ui.repair.apply_btn"))
        self.btn_apply.clicked.connect(self.apply_repairs)
        self.btn_report = QPushButton(tr("ui.repair.open_report"))
        self.btn_report.setEnabled(False)
        rrow.addWidget(self.btn_apply)
        rrow.addWidget(self.btn_report)
        rrow.addStretch(1)
        self.repair.add(rrow)
        self.repair.hide()
        self.lay.addWidget(self.repair)
        self.finish()

    def choose(self):
        f, _ = QFileDialog.getOpenFileName(self, tr("ui.import.choose"), str(Path.home()),
                                           "Timeline (*.json *.zip);;All files (*)")
        if f:
            self.load(f)

    def load(self, path: str):
        if self.worker and self.worker.isRunning():
            return
        self.last_path = path
        self.progress.setValue(0)
        self.progress.show()
        self.banner.set("info", tr("ui.import.loading", name=Path(path).name))
        self.banner.show()
        self.diag.hide()
        self.repair.hide()

        def job(w):
            return load_timeline(path, checkpoint_dir=cache_dir() / "import",
                                 progress=lambda f, m: w.progress.emit(f, m), cancel=w.cancel)

        self.worker = Worker(job, self)
        self.worker.progress.connect(lambda f, m: self.progress.setValue(int(f * 1000)))
        self.worker.done.connect(self.loaded)
        self.worker.failed.connect(self.load_failed)
        self.worker.start()

    def loaded(self, tl):
        self.progress.hide()
        d = tl.diagnostics
        rng = tl.date_range()
        lines = [f"{tr('ui.import.format')}: {d.detected_format}",
                 f"{tr('ui.import.points')}: {len(tl.semantic):,} semantic · {len(tl.raw):,} raw",
                 f"{tr('ui.import.range')}: {rng[0]} → {rng[1]}" if rng else "",
                 f"{tr('ui.import.segments')}: {d.segments_seen:,} · {tr('ui.import.records')}: {d.records_seen:,}",
                 f"{tr('ui.import.dups')}: {d.duplicates_removed:,} · {tr('ui.import.standalone')}: "
                 f"{d.standalone_paths_dropped:,}",
                 f"{tr('ui.import.time')}: {d.parse_seconds:.2f} s"
                 + (f" ({tr('ui.import.resumed')})" if d.resumed_from_checkpoint else "")]
        if d.direction_reversed:
            lines.append(tr("ui.import.reversed"))
        if d.skipped:
            lines.append(tr("ui.import.skipped") + ": " + ", ".join(f"{k.replace('_', ' ')} {v:,}"
                                                                   for k, v in sorted(d.skipped.items())))
        for w in d.warnings:
            lines.append("⚠ " + w)
        self.diag_text.setText("\n".join(l for l in lines if l))
        self.diag.show()
        kind = "warn" if d.skipped else "ok"
        self.banner.set(kind, tr("ui.import.ok_warn" if d.skipped else "ui.import.ok"))
        self.btn_continue.setEnabled(True)
        self.state.set_timeline(tl)

    def load_failed(self, e):
        self.progress.hide()
        msg = str(e) + (f"\n{e.hint}" if isinstance(e, TimelinerXError) and e.hint else "")
        self.banner.set("danger", tr("ui.import.failed") + "\n" + msg)
        self.diag_text.setText(tr("ui.import.failed_help"))
        self.btn_continue.setEnabled(False)
        self.diag.show()

    # --------------------------------------------------------------- repair
    def run_analysis(self):
        if not self.last_path:
            return
        path = self.last_path
        self.banner.set("info", tr("ui.repair.analyzing"))
        self.worker = Worker(lambda w: analyze(path, progress=lambda n: w.progress.emit(0, n)), self)
        self.worker.progress.connect(lambda f, m: self.banner.set("info", tr("ui.repair.pass_running", name=m)))
        self.worker.done.connect(self.show_plan)
        self.worker.failed.connect(lambda e: self.banner.set("danger", str(e)))
        self.worker.start()

    def show_plan(self, plan):
        self.plan = plan
        self.table.setRowCount(0)
        if plan.document is None:
            self.banner.set("danger", tr("ui.repair.unrecoverable") + "\n" +
                            "\n".join(f.message for f in plan.findings))
            return
        for a in plan.actions:
            r = self.table.rowCount()
            self.table.insertRow(r)
            cb = QCheckBox()
            cb.setChecked(a.default_accept)
            cb.setAccessibleName(f"{a.title} {a.location}")
            self.table.setCellWidget(r, 0, cb)
            sym = {"HIGH": "● HIGH", "MEDIUM": "◐ MEDIUM", "LOW": "○ LOW"}[a.confidence.value]
            self.table.setItem(r, 1, QTableWidgetItem(sym))
            self.table.setItem(r, 2, QTableWidgetItem(a.pass_name.replace("_", " ")))
            it = QTableWidgetItem(f"{a.title} — {a.detail}")
            it.setToolTip(f"{a.detail}\nbefore: {a.before}\nafter: {a.after}")
            self.table.setItem(r, 3, it)
            self.table.setItem(r, 4, QTableWidgetItem(a.location))
        s = plan.summary()
        self.banner.set("info" if plan.actions else "ok",
                        tr("ui.repair.summary", high=s["HIGH"], medium=s["MEDIUM"], low=s["LOW"])
                        if plan.actions else tr("ui.repair.nothing"))
        self.repair.show()

    def apply_repairs(self):
        if not self.plan:
            return
        accept, reject = set(), set()
        for r, a in enumerate(self.plan.actions):
            cb = self.table.cellWidget(r, 0)
            (accept if cb.isChecked() else reject).add(a.id)
        plan = self.plan
        self.worker = Worker(lambda w: apply_plan(plan, accept_ids=accept, reject_ids=reject), self)
        self.worker.done.connect(self.repaired)
        self.worker.failed.connect(lambda e: self.banner.set("danger", str(e)))
        self.worker.start()

    def repaired(self, res):
        ok = res.validation.get("ok")
        self.banner.set("ok" if ok else "danger",
                        tr("ui.repair.done", n=len(res.applied), file=res.fixed_path.name) if ok
                        else tr("ui.repair.validation_failed") + " " + str(res.validation.get("error")))
        try:
            self.btn_report.clicked.disconnect()
        except (RuntimeError, TypeError):
            pass
        self.btn_report.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(res.report_html))))
        self.btn_report.setEnabled(True)
        if ok:
            self.load(str(res.fixed_path))
