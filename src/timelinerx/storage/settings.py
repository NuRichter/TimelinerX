"""Application settings, persisted as JSON in the per-user config directory."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import List, Optional

from ..utils.paths import config_dir


@dataclass
class RenderingPrefs:
    ffmpeg_path: str = ""
    default_encoder: str = "auto"
    default_quality: str = "high"
    segment_seconds: float = 4.0
    keep_job_files: bool = False
    accept_encoder_fallback_automatically: bool = False


@dataclass
class MapPrefs:
    default_provider: str = "carto"
    offline_mode: bool = False
    tile_cache_limit_mb: int = 2048
    carto_api_key: str = ""                  # stored only in the local settings file


@dataclass
class PrivacyPrefs:
    debug_log_coordinates: bool = False     # raw coordinates never appear in logs unless enabled


@dataclass
class PerformancePrefs:
    render_threads: int = 0                  # 0 = automatic
    tile_memory_cache: int = 768


@dataclass
class UiPrefs:
    language: str = "en"
    distance_unit: str = "km"                # km | mi — default for new projects and all UI figures
    theme: str = "system"                    # system | light | dark
    reduced_motion: str = "system"           # system | on | off
    advanced_diagnostics: bool = False
    recent_timelines: List[str] = field(default_factory=list)


@dataclass
class StoragePrefs:
    output_dir: str = ""
    watch_folder: str = ""
    watch_enabled: bool = False
    watch_preset_project: str = ""
    update_manifest_url: str = ""            # manual, opt-in update checks only
    load_python_plugins: bool = False        # provider plugins execute code: opt-in


@dataclass
class AppSettings:
    rendering: RenderingPrefs = field(default_factory=RenderingPrefs)
    maps: MapPrefs = field(default_factory=MapPrefs)
    privacy: PrivacyPrefs = field(default_factory=PrivacyPrefs)
    performance: PerformancePrefs = field(default_factory=PerformancePrefs)
    ui: UiPrefs = field(default_factory=UiPrefs)
    storage: StoragePrefs = field(default_factory=StoragePrefs)

    @staticmethod
    def path() -> Path:
        return config_dir() / "settings.json"

    @classmethod
    def load(cls) -> "AppSettings":
        p = cls.path()
        s = cls()
        if not p.is_file():
            return s
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return s
        for f in fields(cls):
            section = getattr(s, f.name)
            for k, v in (data.get(f.name) or {}).items():
                if hasattr(section, k):
                    setattr(section, k, v)
        return s

    def save(self) -> None:
        p = self.path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, p)
