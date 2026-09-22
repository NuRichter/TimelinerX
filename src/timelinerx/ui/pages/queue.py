from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem, QMessageBox,
                               QProgressBar, QPushButton, QTableWidget, QTableWidgetItem)

from ...core.errors import TimelinerXError, RenderCancelledError
from ...i18n import tr
from ...pipeline.render_job import RenderJob
from ...projects.project import Project
from ...utils.paths import jobs_dir
from ..widgets import Banner, Card, ScrollPage, StatTile, muted, page_header, primary_button
from ..workers import ConfirmBridge, Worker


def fmt_eta(s: Optional[float]) -> str:
    if s is None:
        return tr("ui.queue.estimating")
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class QueueEntry:
    def __init__(self, project: Project, output: str, resume_dir: Optional[Path] = None):
        self.project = project
        self.output = output
        self.resume_dir = resume_dir
        self.status = "queued"
        self.job: Optional[RenderJob] = None
        self.worker: Optional[Worker] = None
        self.last_event: dict = {}
        self.nodes: List[dict] = []
        self.error = ""


class QueuePage(ScrollPage):
    def __init__(self, state, main_window):
        super().__init__()
        self.state = state
        self.mw = main_window
        self.entries: List[QueueEntry] = []
        self.active: Optional[QueueEntry] = None
        self.bridge = ConfirmBridge(main_window)
        self.lay.addWidget(page_header(tr("ui.queue.title"), tr("ui.queue.subtitle")))
        self.banner = Banner("info", tr("ui.queue.empty"))
        self.lay.addWidget(self.banner)

        cur = Card(tr("ui.queue.current"))
        self.cur_name = QLabel("—")
        self.cur_name.setObjectName("SectionTitle")
        cur.add(self.cur_name)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setAccessibleName(tr("ui.queue.progress"))
        cur.add(self.bar)
        row = QHBoxLayout()
        self.t = {k: StatTile(tr(f"ui.queue.stat.{k}")) for k in ("progress", "eta", "fps", "frames", "cpu", "ram")}
        for w in self.t.values():
            row.addWidget(w)
        cur.add(row)
        self.stage = muted("")
        cur.add(self.stage)
        brow = QHBoxLayout()
        self.stop_btn = QPushButton(tr("ui.queue.pause"))
        self.stop_btn.setToolTip(tr("ui.queue.pause_tip"))
        self.stop_btn.clicked.connect(lambda: self.stop(keep=True))
        self.cancel_btn = QPushButton(tr("ui.queue.cancel"))
        self.cancel_btn.setProperty("danger", "true")
        self.cancel_btn.clicked.connect(lambda: self.stop(keep=False))
        brow.addWidget(self.stop_btn)
        brow.addWidget(self.cancel_btn)
        brow.addStretch(1)
        cur.add(brow)
        self.lay.addWidget(cur)

        q = Card(tr("ui.queue.list"))
        self.list = QListWidget()
        self.list.setAccessibleName(tr("ui.queue.list"))
        q.add(self.list)
        self.lay.addWidget(q)

        rs = Card(tr("ui.queue.resumable"))
        rs.add(muted(tr("ui.queue.resumable_note")))
        self.resumable = QListWidget()
        rs.add(self.resumable)
        rrow = QHBoxLayout()
        b1 = QPushButton(tr("ui.queue.resume"))
        b1.clicked.connect(self.resume_selected)
        b2 = QPushButton(tr("ui.queue.discard"))
        b2.clicked.connect(self.discard_selected)
        b3 = QPushButton(tr("ui.queue.rescan"))
        b3.clicked.connect(self.scan_resumable)
        for b in (b1, b2, b3):
            rrow.addWidget(b)
        rrow.addStretch(1)
        rs.add(rrow)
        self.lay.addWidget(rs)

        self.inspector = Card(tr("ui.queue.inspector"))
        self.inspector.add(muted(tr("ui.queue.inspector_note")))
        self.graph = QTableWidget(0, 5)
        self.graph.setHorizontalHeaderLabels([tr("ui.queue.node"), tr("ui.queue.status"), tr("ui.queue.deps"),
                                              tr("ui.queue.duration"), tr("ui.queue.detail")])
        self.graph.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.graph.setMinimumHeight(300)
        self.inspector.add(self.graph)
        self.log_btn = QPushButton(tr("ui.queue.open_log"))
        self.log_btn.clicked.connect(self.open_log)
        self.inspector.add(self.log_btn)
        self.lay.addWidget(self.inspector)
        self.finish()
        self.apply_advanced()
        self.scan_resumable()
        self.refresh_controls()

    def apply_advanced(self):
        self.inspector.setVisible(bool(self.state.settings.ui.advanced_diagnostics))

    # ---------------------------------------------------------------- queue
    def enqueue(self, project: Project, output: str, resume_dir: Optional[Path] = None):
        e = QueueEntry(Project.from_dict(project.to_dict()), output, resume_dir)
        self.entries.append(e)
        self.refresh_list()
        self.pump()

    def pump(self):
        if self.active is not None:
            return
        nxt = next((e for e in self.entries if e.status == "queued"), None)
        if nxt is None:
            self.refresh_controls()
            return
        self.start(nxt)

    def start(self, e: QueueEntry):
        self.active = e
        e.status = "running"
        s = self.state.settings

        def listener(ev):
            e.worker.event.emit(ev)

        def run(w):
            pol = self.bridge.policy(accept_all=s.rendering.accept_encoder_fallback_automatically)
            common = dict(policy=pol, listener=listener, cancel=w.cancel, library=self.state.library)
            if e.resume_dir is not None:
                e.job = RenderJob.resume(e.resume_dir, **common)
            else:
                e.job = RenderJob(e.project, e.output, segment_seconds=s.rendering.segment_seconds,
                                  ffmpeg_path=s.rendering.ffmpeg_path or None,
                                  keep_job_files=s.rendering.keep_job_files, **common)
            return e.job.run()

        e.worker = Worker(run, self)
        e.worker.event.connect(lambda ev, e=e: self.on_event(e, ev))
        e.worker.done.connect(lambda res, e=e: self.finished(e, res))
        e.worker.failed.connect(lambda exc, e=e: self.failed(e, exc))
        e.worker.start()
        self.cur_name.setText(f"{e.project.name} → {Path(e.output).name}")
        self.refresh_list()
        self.refresh_controls()

    def on_event(self, e: QueueEntry, ev: dict):
        e.last_event = ev
        eta = ev.get("eta") or {}
        perf = ev.get("perf") or {}
        ov = ev.get("overall")
        if ov is not None:
            self.bar.setValue(int(ov * 1000))
            self.t["progress"].set(f"{ov * 100:.1f} %")
        self.t["eta"].set(fmt_eta(eta.get("eta_s")))
        self.t["fps"].set(f"{eta.get('render_fps') or 0:.1f}")
        self.t["frames"].set(f"{eta.get('frames_done', 0):,}/{eta.get('frames_total', 0):,}")
        if perf.get("available"):
            self.t["cpu"].set(f"{perf.get('cpu_percent', 0):.0f} %")
            self.t["ram"].set(f"{perf.get('app_rss_mb', 0) + perf.get('ffmpeg_rss_mb', 0):,.0f} MB")
        if ev.get("node"):
            self.stage.setText(f"{ev['node']}: {ev.get('message', '')}")
        if ev.get("event") == "fallback":
            d = ev["decision"]
            self.banner.set("warn", tr("ui.queue.fallback", subsystem=d["subsystem"], requested=d["requested"],
                                       proposed=d["proposed"], reason=d["reason"]))
        if e.job and e.job.graph and ev.get("event", "").startswith("node_") and ev["event"] != "node_progress":
            self.show_graph(e.job.graph.snapshot())

    def show_graph(self, nodes: List[dict]):
        self.graph.setRowCount(0)
        sym = {"done": "✔", "cached": "↺", "running": "▶", "failed": "✖", "pending": "·", "skipped": "–",
               "cancelled": "■"}
        for n in nodes:
            r = self.graph.rowCount()
            self.graph.insertRow(r)
            self.graph.setItem(r, 0, QTableWidgetItem(n["label"]))
            self.graph.setItem(r, 1, QTableWidgetItem(f"{sym.get(n['status'], '?')} {n['status']}"))
            self.graph.setItem(r, 2, QTableWidgetItem(", ".join(n["deps"])))
            self.graph.setItem(r, 3, QTableWidgetItem(f"{n['duration_s']:.2f} s" if n["duration_s"] else ""))
            self.graph.setItem(r, 4, QTableWidgetItem((n.get("error") or n.get("message") or "")[:300]))

    def finished(self, e: QueueEntry, res: dict):
        e.status = "done"
        e.nodes = res.get("nodes", [])
        self.show_graph(e.nodes)
        self.banner.set("ok", tr("ui.queue.done", file=res["output"]))
        self.bar.setValue(1000)
        self.stage.setText("")
        self.active = None
        self.state.libraryChanged.emit()
        self.mw.notify(tr("ui.queue.done_title"), Path(res["output"]).name)
        self.refresh_list()
        self.scan_resumable()
        self.pump()

    def failed(self, e: QueueEntry, exc):
        if e.job and e.job.graph:
            self.show_graph(e.job.graph.snapshot())
        if isinstance(exc, RenderCancelledError):
            e.status = "stopped" if getattr(e, "keep", True) else "cancelled"
            if not getattr(e, "keep", True) and e.job is not None:
                e.job.discard()
            self.banner.set("info", tr("ui.queue.stopped") if e.status == "stopped" else tr("ui.queue.cancelled"))
        else:
            e.status = "failed"
            e.error = str(exc)
            hint = f"\n{exc.hint}" if isinstance(exc, TimelinerXError) and exc.hint else ""
            self.banner.set("danger", tr("ui.queue.failed") + f"\n{exc}{hint}\n" + tr("ui.queue.failed_resume"))
        self.active = None
        self.refresh_list()
        self.scan_resumable()
        self.pump()

    def stop(self, keep: bool):
        e = self.active
        if e is None or e.worker is None:
            return
        if not keep:
            r = QMessageBox.question(self, tr("ui.queue.cancel"), tr("ui.queue.cancel_confirm"))
            if r != QMessageBox.Yes:
                return
        e.keep = keep
        e.worker.cancel.cancel(tr("ui.queue.stopped") if keep else tr("ui.queue.cancelled"))

    def refresh_list(self):
        self.list.clear()
        for e in self.entries:
            sym = {"queued": "·", "running": "▶", "done": "✔", "failed": "✖", "stopped": "❚❚", "cancelled": "■"}
            it = QListWidgetItem(f"{sym.get(e.status, '?')} {tr('ui.queue.state.' + e.status)} — "
                                 f"{e.project.name} → {e.output}")
            if e.error:
                it.setToolTip(e.error)
            self.list.addItem(it)
        if self.active is None and not any(e.status == "queued" for e in self.entries) and not self.entries:
            self.banner.set("info", tr("ui.queue.empty"))

    def refresh_controls(self):
        running = self.active is not None
        self.stop_btn.setEnabled(running)
        self.cancel_btn.setEnabled(running)

    # ----------------------------------------------------------- resumable
    def scan_resumable(self):
        self.resumable.clear()
        active_dir = str(self.active.job.job_dir) if self.active and self.active.job else None
        for d in sorted(jobs_dir().iterdir()) if jobs_dir().exists() else []:
            if not (d / "job.json").is_file() or str(d) == active_dir:
                continue
            try:
                m = json.loads((d / "job.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            segs = 0
            try:
                segs = len(json.loads((d / "segments" / "manifest.json").read_text(encoding="utf-8"))["segments"])
            except (OSError, ValueError, KeyError):
                pass
            it = QListWidgetItem(f"{Path(m['output']).name} — {m.get('created_at', '')[:19]} — "
                                 f"{tr('ui.queue.segments_done', n=segs)}")
            it.setData(Qt.UserRole, str(d))
            self.resumable.addItem(it)

    def resume_selected(self):
        it = self.resumable.currentItem()
        if not it:
            return
        d = Path(it.data(Qt.UserRole))
        m = json.loads((d / "job.json").read_text(encoding="utf-8"))
        self.enqueue(Project.from_dict(m["project"]), m["output"], resume_dir=d)

    def discard_selected(self):
        it = self.resumable.currentItem()
        if not it:
            return
        if QMessageBox.question(self, tr("ui.queue.discard"), tr("ui.queue.discard_confirm")) != QMessageBox.Yes:
            return
        import shutil
        shutil.rmtree(it.data(Qt.UserRole), ignore_errors=True)
        self.scan_resumable()

    def open_log(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        e = self.active or (self.entries[-1] if self.entries else None)
        if e and e.job:
            p = e.job.job_dir / "ffmpeg.log"
            if p.exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
                return
        from ...utils.paths import logs_dir
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(logs_dir())))

    def busy(self) -> bool:
        return self.active is not None
