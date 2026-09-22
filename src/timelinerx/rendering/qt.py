"""Qt bootstrap for rendering (works headless for the CLI)."""

from __future__ import annotations

import os
import threading
from typing import Dict

_lock = threading.Lock()
_FAMILIES: Dict[str, str] = {}


def ensure_qt_app():
    """Return the running Q(Gui)Application, creating an offscreen one if needed."""
    from PySide6.QtGui import QGuiApplication

    with _lock:
        app = QGuiApplication.instance()
        if app is None:
            if threading.current_thread() is not threading.main_thread():
                raise RuntimeError("Qt must be initialised on the main thread: call ensure_qt_app() "
                                   "before starting render threads.")
            if not os.environ.get("QT_QPA_PLATFORM"):
                os.environ["QT_QPA_PLATFORM"] = "offscreen"
            app = QGuiApplication(["timelinerx-render"])
        _register_fonts()
        return app


def _register_fonts() -> None:
    if _FAMILIES:
        return
    from PySide6.QtGui import QFontDatabase

    from ..utils.paths import assets_dir

    fdir = assets_dir() / "fonts"
    for f in sorted(fdir.glob("*.ttf")):
        fid = QFontDatabase.addApplicationFont(str(f))
        if fid >= 0:
            fams = QFontDatabase.applicationFontFamilies(fid)
            if fams:
                _FAMILIES[f.stem] = fams[0]


# Per-language fallbacks for scripts the bundled Latin fonts do not cover. Qt picks glyphs from
# the list in order, so Latin text keeps the bundled face and Cyrillic/Arabic/CJK come from a
# good system font (Windows first, then macOS/Linux equivalents, then bundled DejaVu).
SCRIPT_FALLBACKS = {
    "ru": ["Segoe UI", "Helvetica Neue", "Noto Sans", "DejaVu Sans"],
    "ar": ["Segoe UI", "Geeza Pro", "Noto Sans Arabic", "Noto Naskh Arabic", "DejaVu Sans"],
    "zh": ["Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "Noto Sans SC"],
    "ja": ["Yu Gothic UI", "Meiryo UI", "Hiragino Sans", "Noto Sans CJK JP", "Noto Sans JP"],
}


def families(kind: str = "ui", lang: str | None = None) -> list:
    """Font family list for ``QFont.setFamilies`` / QSS: bundled face first, then script
    fallbacks for ``lang`` (default: the active UI language)."""
    if lang is None:
        from ..i18n import language
        lang = language()
    out = [family(kind)]
    for f in SCRIPT_FALLBACKS.get(lang, []):
        if f not in out:
            out.append(f)
    if "DejaVu Sans" not in out:
        out.append("DejaVu Sans")
    return out


def font(kind: str = "ui", lang: str | None = None):
    from PySide6.QtGui import QFont
    q = QFont()
    q.setFamilies(families(kind, lang))
    return q


def family(kind: str = "ui") -> str:
    """Bundled font family names; deterministic across machines."""
    _register_fonts()
    if kind == "display":
        return _FAMILIES.get("Outfit-Bold", _FAMILIES.get("Outfit-Regular", "DejaVu Sans"))
    return _FAMILIES.get("InstrumentSans-Regular", "DejaVu Sans")
