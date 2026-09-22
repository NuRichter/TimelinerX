"""Filesystem locations: bundled assets, per-user data, cache and config.

Environment overrides (useful for portable installs and tests):
``TIMELINERX_HOME`` puts data, cache and config under one directory
(``NURICHTER_HOME`` from the 0.x releases is still honoured).

Per-user folders of the 0.x releases ("NuRichterTimeliner") are copied to the
new "TimelinerX" location once, on first start; the old folders are left in
place untouched.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import shutil

from .. import APP_ID, LEGACY_APP_ID

try:
    import platformdirs
except Exception:  # pragma: no cover
    platformdirs = None


def assets_dir() -> Path:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "assets"
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "assets" / "themes").is_dir():
            return parent / "assets"
    return here.parents[3] / "assets"


def _home() -> Path | None:
    h = os.environ.get("TIMELINERX_HOME") or os.environ.get("NURICHTER_HOME")
    return Path(h) if h else None


def _user_dir(kind: str, app_id: str) -> Path:
    if platformdirs:
        return Path(getattr(platformdirs, f"user_{kind}_dir")(app_id, False))
    return Path.home() / f".{app_id}" / kind


def _migrated(kind: str) -> Path:
    """New per-user folder; seeded once from the 0.x folder when that exists."""
    new = _user_dir(kind, APP_ID)
    if not new.exists():
        old = _user_dir(kind, LEGACY_APP_ID)
        if old.is_dir() and kind != "cache":
            try:
                shutil.copytree(old, new, ignore=shutil.ignore_patterns("jobs", "*.lock"))
            except OSError:
                pass   # migration is best-effort; a fresh folder is created below
    return new


def data_dir() -> Path:
    h = _home()
    p = h / "data" if h else _migrated("data")
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    h = _home()
    p = h / "cache" if h else _migrated("cache")
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_dir() -> Path:
    h = _home()
    p = h / "config" if h else _migrated("config")
    p.mkdir(parents=True, exist_ok=True)
    return p


def plugins_dir() -> Path:
    p = data_dir() / "plugins"
    p.mkdir(parents=True, exist_ok=True)
    return p


def jobs_dir() -> Path:
    p = data_dir() / "jobs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path:
    p = data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def safe_filename(name: str, default: str = "untitled", max_len: int = 120) -> str:
    """Make a user-supplied title safe as a Windows/Linux file name."""
    s = _UNSAFE.sub("_", name).strip().strip(".")
    s = re.sub(r"\s+", " ", s)[:max_len].rstrip(" .")
    if not s:
        s = default
    if s.split(".")[0].upper() in _RESERVED:
        s = "_" + s
    return s


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
