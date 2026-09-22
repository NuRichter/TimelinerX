"""Shared application state with change signals."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal

from ..projects.project import Project, TimelineRef
from ..storage.library import Library
from ..storage.settings import AppSettings
from ..timeline.model import Timeline


class AppState(QObject):
    timelineChanged = Signal()
    projectChanged = Signal()
    libraryChanged = Signal()
    settingsChanged = Signal()
    envChanged = Signal()
    navigate = Signal(str)
    status = Signal(str)

    def __init__(self, settings: AppSettings):
        super().__init__()
        self.settings = settings
        self.timeline: Optional[Timeline] = None
        self.project = Project()
        self.project.video.encoder = settings.rendering.default_encoder
        self.project.video.quality = settings.rendering.default_quality
        self.project.visual.map_provider = settings.maps.default_provider
        self.project.visual.offline = settings.maps.offline_mode
        self.project.title.language = settings.ui.language
        self.project.title.unit = settings.ui.distance_unit
        self.project_path: Optional[Path] = None
        self.library = Library()
        self.env_report = None

    def set_timeline(self, tl: Timeline) -> None:
        self.timeline = tl
        p = Path(tl.source_path)
        self.project.timeline = TimelineRef(str(p), tl.source_sha256 or "", p.stat().st_size)
        if self.project.name in ("", "Untitled journey"):
            self.project.name = p.stem
        rng = tl.date_range()
        if rng and not self.project.period.start:
            self.project.period.start, self.project.period.end = str(rng[0]), str(rng[1])
        rec = self.settings.ui.recent_timelines
        if str(p) in rec:
            rec.remove(str(p))
        rec.insert(0, str(p))
        del rec[8:]
        self.settings.save()
        self.timelineChanged.emit()
        self.projectChanged.emit()

    def set_project(self, proj: Project, path: Optional[Path] = None) -> None:
        self.project = proj
        self.project_path = path
        self.projectChanged.emit()

    def touch_project(self) -> None:
        self.projectChanged.emit()
