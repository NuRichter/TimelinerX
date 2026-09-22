"""Project model (``.nrproj``) — every user-visible setting, validated.

A project references its Timeline by path + SHA-256; it never copies the
Timeline content. Unknown keys are rejected so typos never silently fall back
to defaults.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import RENDER_ENGINE_VERSION, __version__
from ..core.errors import ProjectError, UsageError

PROJECT_FORMAT = "timelinerx-project"
LEGACY_PROJECT_FORMATS = ("nurichter-project",)   # 0.x files open unchanged
SCHEMA_VERSION = 1

RESOLUTIONS = ["480p", "720p", "1080p", "1440p", "2160p", "4K", "8K"]
PREVIEW_RESOLUTIONS = ["480p", "720p", "1080p"]
FPS_CHOICES = [24, 30, 60]
ASPECTS = {"16:9": (16, 9), "9:16": (9, 16), "1:1": (1, 1)}
QUALITY = ["draft", "standard", "high", "cinematic"]
TITLE_LAYOUTS = ["corner", "centered", "minimal", "lower_third", "none"]


def resolve_dimensions(resolution: str, aspect: str, custom: Optional[Tuple[int, int]] = None
                       ) -> Tuple[int, int]:
    """Resolution presets.

    ``480p``…``2160p`` fix the *short* edge (so 1080p 9:16 is 1080×1920).
    ``4K`` is DCI-style long edge 4096 and ``8K`` long edge 7680.
    Custom dimensions must be even and within 128…8192.
    """
    if aspect == "custom":
        if not custom:
            raise UsageError("Custom aspect needs explicit width and height.")
        w, h = int(custom[0]), int(custom[1])
    else:
        if aspect not in ASPECTS:
            raise UsageError(f"Unknown aspect ratio {aspect!r}; choose {', '.join(ASPECTS)} or custom.")
        aw, ah = ASPECTS[aspect]
        if resolution in ("4K", "8K"):
            long_edge = 4096 if resolution == "4K" else 7680
            if aw >= ah:
                w, h = long_edge, long_edge * ah // aw
            else:
                h, w = long_edge, long_edge * aw // ah
        elif resolution.endswith("p") and resolution[:-1].isdigit():
            short = int(resolution[:-1])
            if short not in (480, 720, 1080, 1440, 2160):
                raise UsageError(f"Unsupported resolution {resolution!r}.")
            if aw >= ah:
                h, w = short, short * aw // ah
            else:
                w, h = short, short * ah // aw
        else:
            raise UsageError(f"Unsupported resolution {resolution!r}.")
        w -= w % 2
        h -= h % 2
    if w % 2 or h % 2:
        raise UsageError(f"Video dimensions must be even (got {w}×{h}).")
    if not (128 <= w <= 8192 and 128 <= h <= 8192):
        raise UsageError(f"Video dimensions {w}×{h} are outside 128…8192.")
    return w, h


@dataclass
class TimelineRef:
    path: str = ""
    sha256: str = ""
    size: int = 0


@dataclass
class PeriodSettings:
    start: Optional[str] = None   # YYYY-MM-DD (local calendar)
    end: Optional[str] = None

    def dates(self) -> Tuple[Optional[date], Optional[date]]:
        def p(v):
            return date.fromisoformat(v) if v else None
        try:
            s, e = p(self.start), p(self.end)
        except ValueError as ex:
            raise UsageError(f"Invalid date in period: {ex}") from ex
        if s and e and e < s:
            raise UsageError("Period end is before period start.")
        return s, e


@dataclass
class JourneySettings:
    route_source: str = "semantic"
    outlier_filter: str = "conservative"
    trip_detection: str = "balanced"


@dataclass
class CameraSettings:
    mode: str = "active"            # Zoom style: fixed | balanced | active | close_up
    composition: str = "thirds"
    local_framing: str = "balanced"  # Local trip framing: off | balanced | close
    pacing: str = "visual_zoom"
    compression: str = "balanced"    # Long-trip pacing: natural | balanced | faster | fastest
    smoothing: float = 1.0
    anticipation: float = 1.0
    intro_s: float = 1.2
    outro_transition_s: float = 1.2
    outro_hold_s: float = 1.2
    keyframes: List[dict] = field(default_factory=list)


@dataclass
class TrailSettings:
    length: float = 1.0          # multiplier on the default recent-trail length
    width: float = 1.0
    gradient: bool = True
    glow: bool = True
    shadow: bool = True
    pulse: bool = True
    show_full_route: bool = True


@dataclass
class VisualSettings:
    theme: str = "dark"
    map_provider: str = "carto"   # carto (flavour follows theme) | carto-light | carto-dark | carto-voyager | plain | mbtiles:<path>
    trail: TrailSettings = field(default_factory=TrailSettings)
    vignette: bool = True
    grain: bool = True
    offline: bool = False         # never download tiles; use cache only


@dataclass
class TitleSettings:
    template: str = "{name} · {year}"
    name: str = ""
    layout: str = "corner"
    show_date: bool = True
    date_format: str = "month"    # month | day
    show_distance: bool = True
    ending_title: bool = True
    ending_template: str = "{distance} · {trips} trips · {days} days"
    timeline_bar: bool = True          # date ribbon along the bottom edge
    count_up: bool = True              # ending-card figures count up as the card appears
    unit: str = "km"
    language: str = "en"


@dataclass
class VideoSettings:
    resolution: str = "1080p"
    aspect: str = "16:9"
    width: Optional[int] = None
    height: Optional[int] = None
    fps: int = 30
    duration_s: float = 30.0
    quality: str = "high"
    codec: str = "h264"            # h264 | hevc
    encoder: str = "auto"          # auto | nvenc | qsv | amf | software
    two_pass: bool = False
    hdr: bool = False              # experimental HDR10 (PQ) container export
    motion_blur: bool = False      # 180° shutter, adaptive sub-frames (slower render)

    def dimensions(self) -> Tuple[int, int]:
        custom = (self.width, self.height) if self.width and self.height else None
        return resolve_dimensions(self.resolution, self.aspect, custom)


@dataclass
class AudioSettings:
    enabled: bool = False
    path: Optional[str] = None
    volume: float = 1.0
    beat_sync: bool = False
    ducking: bool = False
    duck_db: float = -8.0
    fade_out_s: float = 2.0


@dataclass
class Project:
    name: str = "Untitled journey"
    timeline: TimelineRef = field(default_factory=TimelineRef)
    period: PeriodSettings = field(default_factory=PeriodSettings)
    journey: JourneySettings = field(default_factory=JourneySettings)
    camera: CameraSettings = field(default_factory=CameraSettings)
    visual: VisualSettings = field(default_factory=VisualSettings)
    title: TitleSettings = field(default_factory=TitleSettings)
    video: VideoSettings = field(default_factory=VideoSettings)
    audio: AudioSettings = field(default_factory=AudioSettings)
    output_dir: Optional[str] = None
    render_engine_version: str = RENDER_ENGINE_VERSION

    # ------------------------------------------------------------ validation
    def validate(self) -> List[str]:
        errs: List[str] = []
        v = self.video
        try:
            v.dimensions()
        except UsageError as e:
            errs.append(str(e))
        if v.fps not in FPS_CHOICES:
            errs.append(f"FPS must be one of {FPS_CHOICES} (got {v.fps}).")
        if not (5.0 <= v.duration_s <= 1800.0):
            errs.append("Duration must be between 5 and 1800 seconds.")
        if v.quality not in QUALITY:
            errs.append(f"Quality must be one of {QUALITY}.")
        if v.codec not in ("h264", "hevc"):
            errs.append("Codec must be h264 or hevc.")
        if v.encoder not in ("auto", "nvenc", "qsv", "amf", "software"):
            errs.append("Encoder must be auto, nvenc, qsv, amf or software.")
        if v.hdr and v.codec != "hevc":
            errs.append("HDR export requires the HEVC codec.")
        from ..camera.planner import CAMERA_MODES, LONG_TRIP_PACING, LOCAL_FRAMING
        if self.camera.mode not in CAMERA_MODES:
            errs.append(f"Zoom style must be one of {list(CAMERA_MODES)}.")
        if self.camera.compression not in LONG_TRIP_PACING:
            errs.append(f"Long-trip pacing must be one of {list(LONG_TRIP_PACING)}.")
        if self.camera.local_framing not in LOCAL_FRAMING:
            errs.append(f"Local trip framing must be one of {list(LOCAL_FRAMING)}.")
        if self.journey.trip_detection not in ("conservative", "balanced", "sensitive"):
            errs.append("Long-trip detection must be conservative, balanced or sensitive.")
        if self.camera.composition not in ("thirds", "centered"):
            errs.append("Composition must be thirds or centered.")
        if self.camera.pacing not in ("visual_zoom", "visual", "distance"):
            errs.append("Pacing must be visual_zoom, visual or distance.")
        if self.journey.route_source not in ("semantic", "detailed"):
            errs.append("Route source must be semantic or detailed.")
        if self.journey.outlier_filter not in ("conservative", "off"):
            errs.append("Outlier filter must be conservative or off.")
        if self.title.layout not in TITLE_LAYOUTS:
            errs.append(f"Title layout must be one of {TITLE_LAYOUTS}.")
        if self.title.unit not in ("km", "mi"):
            errs.append("Unit must be km or mi.")
        if self.audio.enabled and not self.audio.path:
            errs.append("Audio is enabled but no audio file is selected.")
        if self.audio.enabled and self.audio.path and not Path(self.audio.path).is_file():
            errs.append(f"Audio file not found: {self.audio.path}")
        try:
            self.period.dates()
        except UsageError as e:
            errs.append(str(e))
        for i, kf in enumerate(self.camera.keyframes):
            for k in ("time_s", "lat", "lon", "span_km"):
                if k not in kf:
                    errs.append(f"Keyframe {i + 1} is missing '{k}'.")
        return errs

    # ----------------------------------------------------------- persistence
    def to_dict(self) -> dict:
        d = asdict(self)
        return {"format": PROJECT_FORMAT, "schema_version": SCHEMA_VERSION,
                "app_version": __version__, **d}

    def save(self, path: str | os.PathLike) -> Path:
        p = Path(path)
        if p.suffix.lower() != ".nrproj":
            p = p.with_suffix(".nrproj")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".nrproj.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
        return p

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        if d.get("format") != PROJECT_FORMAT and d.get("format") not in LEGACY_PROJECT_FORMATS:
            raise ProjectError("Not a TimelinerX project file.")
        if int(d.get("schema_version", 0)) > SCHEMA_VERSION:
            raise ProjectError("This project was created by a newer version of TimelinerX.")
        body = {k: v for k, v in d.items() if k not in ("format", "schema_version", "app_version")}
        proj = _build(cls, body, "project")
        _migrate(proj)
        return proj

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Project":
        p = Path(path)
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise ProjectError(f"Cannot read project {p}: {e}") from e
        proj = cls.from_dict(d)
        # resolve a relative timeline path against the project location
        if proj.timeline.path and not Path(proj.timeline.path).is_absolute():
            proj.timeline.path = str((p.parent / proj.timeline.path).resolve())
        return proj


def _migrate(proj: "Project") -> None:
    """Map 0.x option names onto the current ones (the meaning is preserved)."""
    from ..camera.planner import LEGACY_CAMERA_MODES, LEGACY_PACING
    proj.camera.mode = LEGACY_CAMERA_MODES.get(proj.camera.mode, proj.camera.mode)
    proj.camera.compression = LEGACY_PACING.get(proj.camera.compression, proj.camera.compression)


def _build(tp, data: Any, where: str):
    if not is_dataclass(tp):
        return data
    if not isinstance(data, dict):
        raise ProjectError(f"{where}: expected an object.")
    known = {f.name: f for f in fields(tp)}
    unknown = set(data) - set(known)
    if unknown:
        raise ProjectError(f"{where}: unknown setting(s) {', '.join(sorted(unknown))}.")
    kwargs = {}
    hints = _hints(tp)
    for name, val in data.items():
        ftype = hints.get(name)
        if is_dataclass(ftype):
            kwargs[name] = _build(ftype, val, f"{where}.{name}")
        else:
            kwargs[name] = val
    return tp(**kwargs)


def _hints(tp):
    import typing
    try:
        return typing.get_type_hints(tp)
    except Exception:  # pragma: no cover
        return {}
