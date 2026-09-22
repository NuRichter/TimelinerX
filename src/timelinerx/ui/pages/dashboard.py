from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton

from ...i18n import tr
from ..widgets import Banner, Card, ScrollPage, StatTile, muted, page_header, primary_button


class DashboardPage(ScrollPage):
    def __init__(self, state, open_timeline, open_project):
        super().__init__()
        self.state = state
        self.lay.addWidget(page_header(tr("ui.dashboard.title"), tr("ui.dashboard.subtitle")))

        actions = QHBoxLayout()
        b1 = primary_button(tr("ui.dashboard.import"))
        b1.clicked.connect(open_timeline)
        b2 = QPushButton(tr("ui.dashboard.open_project"))
        b2.clicked.connect(open_project)
        actions.addWidget(b1)
        actions.addWidget(b2)
        actions.addStretch(1)
        self.lay.addLayout(actions)
        from ..tutorial import TutorialCard
        self.tutorial = TutorialCard(state)
        self.lay.addWidget(self.tutorial)

        self.env_banner = Banner("info", tr("ui.dashboard.scanning"))
        self.lay.addWidget(self.env_banner)

        grid = QGridLayout()
        grid.setSpacing(16)
        env = Card(tr("ui.dashboard.env_title"))
        self.tiles = {k: StatTile(tr(f"ui.dashboard.env.{k}")) for k in ("profile", "encoder", "ram", "gpu")}
        row = QHBoxLayout()
        for t in self.tiles.values():
            row.addWidget(t)
        env.add(row)
        env.add(muted(tr("ui.dashboard.env_disclaimer")))
        self.env_details = muted("")
        env.add(self.env_details)
        grid.addWidget(env, 0, 0, 1, 2)

        rec = Card(tr("ui.dashboard.recent_timelines"))
        self.recent = QListWidget()
        self.recent.setAccessibleName(tr("ui.dashboard.recent_timelines"))
        self.recent.itemActivated.connect(lambda it: open_timeline(it.data(Qt.UserRole)))
        rec.add(self.recent)
        grid.addWidget(rec, 1, 0)

        vids = Card(tr("ui.dashboard.recent_videos"))
        self.videos = QListWidget()
        self.videos.setAccessibleName(tr("ui.dashboard.recent_videos"))
        self.videos.itemActivated.connect(lambda it: state.navigate.emit("library"))
        vids.add(self.videos)
        grid.addWidget(vids, 1, 1)
        self.lay.addLayout(grid)
        self.finish()
        state.envChanged.connect(self.refresh_env)
        state.libraryChanged.connect(self.refresh)
        state.timelineChanged.connect(self.refresh)
        self.refresh()

    def refresh(self):
        self.recent.clear()
        for p in self.state.settings.ui.recent_timelines:
            it = QListWidgetItem(Path(p).name + ("" if Path(p).exists() else f"  ({tr('ui.missing')})"))
            it.setToolTip(p)
            it.setData(Qt.UserRole, p)
            self.recent.addItem(it)
        if not self.state.settings.ui.recent_timelines:
            self.recent.addItem(tr("ui.dashboard.none_yet"))
        self.videos.clear()
        for v in self.state.library.list()[:8]:
            self.videos.addItem(f"{v.title} — {v.width}×{v.height} · {v.fps} fps · {v.duration_s:.0f}s")
        if not self.videos.count():
            self.videos.addItem(tr("ui.dashboard.none_yet"))

    def refresh_env(self):
        r = self.state.env_report
        if r is None:
            return
        self.tiles["profile"].set(tr(f"ui.profile.{r.profile.lower()}"))
        ff = r.ffmpeg or {}
        hw = [e for e, ok in ff.get("hw_verified", {}).items() if ok]
        self.tiles["encoder"].set(hw[0] if hw else ("libx264" if ff else "—"))
        self.tiles["ram"].set(f"{r.ram_total_gb or 0:.0f} GB")
        g = r.gpus[0].name if r.gpus else tr("ui.none_detected")
        self.tiles["gpu"].set(g[:28])
        if r.ffmpeg_error:
            self.env_banner.set("danger", tr("ui.dashboard.ffmpeg_missing") + "\n" + r.ffmpeg_error)
        elif not hw:
            self.env_banner.set("warn", tr("ui.dashboard.no_hw"))
        else:
            self.env_banner.set("ok", tr("ui.dashboard.env_ok", enc=", ".join(hw)))
        self.env_details.setText("\n".join(r.profile_reasons))
