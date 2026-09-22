"""Plugin SDK loader (Section VIII.3).

Plugins live in ``<user data>/plugins`` (never downloaded automatically):

* ``plugins/themes/*.json`` — theme node graphs (validated by
  :func:`rendering.styles.validate_theme_dict`).
* ``plugins/providers/*.py`` — map providers. A module must define
  ``PROVIDER_ID`` (str) and ``create_provider()`` returning an object that
  satisfies :class:`maps.tiles.MapProvider` (``fetch_tile``, ``attribution``,
  ``rate_limit``, ``id``, ``name``, ``max_zoom``, ``tile_size``,
  ``base_flavor``). Providers must attribute their data: an empty attribution
  is rejected.

Python provider plugins execute code, so they are loaded only when the user
enables "Load provider plugins" in Settings; every failure is reported with a
diagnostic instead of crashing the app.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List

from ..maps.tiles import RateLimitPolicy, TileResult
from ..rendering.styles import THEME_LOAD_ERRORS, load_themes
from ..utils.paths import plugins_dir

REQUIRED_ATTRS = ("id", "name", "max_zoom", "tile_size", "base_flavor")
REQUIRED_METHODS = ("fetch_tile", "attribution", "rate_limit")


@dataclass
class PluginReport:
    providers: Dict[str, Callable] = field(default_factory=dict)
    themes: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def validate_provider(obj) -> List[str]:
    errs = []
    for a in REQUIRED_ATTRS:
        if not hasattr(obj, a):
            errs.append(f"missing attribute '{a}'")
    for m in REQUIRED_METHODS:
        if not callable(getattr(obj, m, None)):
            errs.append(f"missing method '{m}()'")
    if errs:
        return errs
    try:
        if not isinstance(obj.attribution(), str) or not obj.attribution().strip():
            errs.append("attribution() must return a non-empty string")
        if not isinstance(obj.rate_limit(), RateLimitPolicy):
            errs.append("rate_limit() must return RateLimitPolicy")
        if not (isinstance(obj.max_zoom, int) and 0 < obj.max_zoom <= 24):
            errs.append("max_zoom must be an int in 1..24")
        if obj.tile_size not in (256, 512):
            errs.append("tile_size must be 256 or 512")
        if obj.base_flavor not in ("light", "dark", "none"):
            errs.append("base_flavor must be light, dark or none")
    except Exception as e:  # noqa: BLE001
        errs.append(f"contract check raised {type(e).__name__}: {e}")
    return errs


def load_plugins(directory: Path | None = None, load_python: bool = False) -> PluginReport:
    rep = PluginReport()
    root = Path(directory) if directory else plugins_dir()
    themes = load_themes(extra_dirs=[root / "themes"] if directory else None, reload=True)
    rep.themes = [t.id for t in themes.values() if t.source.startswith("plugin")]
    rep.errors.extend(f"theme {e}" for e in THEME_LOAD_ERRORS)
    pdir = root / "providers"
    if not pdir.is_dir():
        return rep
    for f in sorted(pdir.glob("*.py")):
        if not load_python:
            rep.errors.append(f"provider {f.name}: not loaded (provider plugins are disabled in Settings)")
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"timelinerx_plugin_{f.stem}", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            pid = getattr(mod, "PROVIDER_ID", None)
            factory = getattr(mod, "create_provider", None)
            if not isinstance(pid, str) or not callable(factory):
                raise ValueError("module must define PROVIDER_ID and create_provider()")
            if pid in ("carto", "carto-light", "carto-dark", "carto-voyager", "plain") or pid.startswith("mbtiles:"):
                raise ValueError(f"PROVIDER_ID '{pid}' collides with a built-in provider")
            inst = factory()
            errs = validate_provider(inst)
            if errs:
                raise ValueError("; ".join(errs))
            rep.providers[pid] = factory
            from ..maps.tiles import PLUGIN_PROVIDERS
            PLUGIN_PROVIDERS[pid] = factory
        except Exception as e:  # noqa: BLE001 — a broken plugin must not crash the app
            rep.errors.append(f"provider {f.name}: {e}")
    return rep


__all__ = ["load_plugins", "validate_provider", "PluginReport", "TileResult", "RateLimitPolicy"]
