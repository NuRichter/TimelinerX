"""Preview: the exact render path at a capped resolution (480p/720p/1080p).

The preview uses the same journey builder, camera planner, compositor and
frame renderer as the final render — only the output size differs (and it is
capped). Missing map tiles are drawn as placeholders *and counted*; the UI
shows that count, so the preview never silently looks different from what
the render will do.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from ..camera.planner import plan_frames
from ..core.errors import UsageError
from ..journeys.journey import build_journey
from ..projects.project import PREVIEW_RESOLUTIONS, Project
from ..timeline.model import Timeline
from ..timeline.parser import load_timeline
from ..maps.tiles import make_provider, missing_key
from .render_job import build_renderer, camera_config, journey_config, provider_spec

_TL_CACHE: Dict[str, Timeline] = {}


def preview_dimensions(project: Project, preview_res: str) -> tuple:
    if preview_res not in PREVIEW_RESOLUTIONS:
        raise UsageError(f"Preview is limited to {', '.join(PREVIEW_RESOLUTIONS)} "
                         f"(requested {preview_res}).")
    w, h = project.video.dimensions()
    short = int(preview_res[:-1])
    k = short / min(w, h)
    if k >= 1:
        return w, h
    pw = int(round(w * k)) // 2 * 2
    ph = int(round(h * k)) // 2 * 2
    return pw, ph


def cached_timeline(path: str, sha: str = "") -> Timeline:
    key = f"{path}|{sha}"
    if key not in _TL_CACHE:
        _TL_CACHE.clear()
        _TL_CACHE[key] = load_timeline(path, compute_sha=False)
    return _TL_CACHE[key]


class PreviewSession:
    def __init__(self, project: Project, preview_res: str = "720p", *, allow_placeholder: bool = True,
                 timeline: Optional[Timeline] = None, tile_root: Optional[Path] = None):
        self.project = project
        self.width, self.height = preview_dimensions(project, preview_res)
        tl = timeline or cached_timeline(project.timeline.path, project.timeline.sha256)
        self.journey = build_journey(tl, journey_config(project))
        self.plan = plan_frames(self.journey, camera_config(project), width=self.width, height=self.height,
                                fps=project.video.fps, duration_s=project.video.duration_s)
        # A CARTO map without an API key would show watermarked tiles. The preview then draws the
        # plain background instead and says so (``notice``); the final render refuses outright.
        self.notice: Optional[str] = None
        force_plain = missing_key(make_provider(provider_spec(project)))
        if force_plain:
            self.notice = "carto_key_missing"
        self.renderer, self.comp, self.cache = build_renderer(project, self.journey, self.plan,
                                                              allow_placeholder=allow_placeholder,
                                                              tile_root=tile_root, force_plain=force_plain,
                                                              motion_blur=False)   # interactive: off

    @property
    def missing_tiles(self) -> int:
        return len(self.comp.missing)

    def render_frame(self, index: int):
        index = max(0, min(self.plan.frame_count - 1, int(index)))
        return self.renderer.render(index)
