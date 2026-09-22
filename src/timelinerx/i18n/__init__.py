"""Internationalisation. Strings live in ``i18n/<lang>.json`` — never in code.

``tr("key", **fmt)`` looks the key up in the active language, then English,
then returns the key itself (so a missing translation is visible, not silent).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

_DIR = Path(__file__).resolve().parent
_CACHE: Dict[str, Dict[str, str]] = {}
_active = "en"
# Native names, shown as-is in the language picker.
LANGUAGES = {"en": "English", "id": "Bahasa Indonesia", "es": "Español", "fr": "Français", "de": "Deutsch",
             "pt": "Português (Brasil)", "ru": "Русский", "ar": "العربية", "zh": "中文（简体）", "ja": "日本語"}
RTL_LANGUAGES = {"ar"}


def is_rtl(lang: str | None = None) -> bool:
    return (lang or _active) in RTL_LANGUAGES


def _load(lang: str) -> Dict[str, str]:
    if lang not in _CACHE:
        p = _DIR / f"{lang}.json"
        try:
            _CACHE[lang] = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _CACHE[lang] = {}
    return _CACHE[lang]


def set_language(lang: str) -> None:
    global _active
    _active = lang if lang in LANGUAGES else "en"


def language() -> str:
    return _active


def tr(key: str, lang: str | None = None, **fmt) -> str:
    table = _load(lang or _active)
    s = table.get(key)
    if s is None:
        s = _load("en").get(key, key)
    if fmt:
        try:
            return s.format(**fmt)
        except (KeyError, IndexError, ValueError):
            return s
    return s


def month_name(month: int, lang: str | None = None, short: bool = False, genitive: bool = False) -> str:
    """Month name. ``genitive`` is the form used with a day number (Russian "5 июня"); languages
    without a separate form fall back to the nominative."""
    if short:
        return tr(f"month.short.{month}", lang)
    if genitive:
        table = _load(lang or _active)
        g = table.get(f"month.gen.{month}")
        if g:
            return g
    return tr(f"month.{month}", lang)


def format_date(dt, lang: str | None = None, with_day: bool = True, short: bool = False) -> str:
    """Localised calendar date from the language's own pattern (word order, particles)."""
    if with_day:
        pat = tr("video.date.short_day" if short else "video.date.day", lang)
        return pat.format(day=dt.day, month=month_name(dt.month, lang, short=short, genitive=not short),
                          year=dt.year, m=dt.month)
    return tr("video.date.month", lang).format(month=month_name(dt.month, lang), year=dt.year, m=dt.month)


# Digit grouping / decimal separators per UI language (Western digits everywhere, so figures in
# videos stay comparable across languages).
_SEP = {"en": (",", "."), "id": (".", ","), "es": (".", ","), "fr": ("\u202f", ","), "de": (".", ","),
        "ru": ("\u00a0", ","), "ja": (",", "."), "zh": (",", "."), "ar": ("\u066c", "\u066b"),
        "pt": (".", ",")}


def format_number(x: float, decimals: int = 0, lang: str | None = None) -> str:
    group, dec = _SEP.get(lang or _active, _SEP["en"])
    s = f"{x:,.{decimals}f}"
    return s.replace(",", "\x00").replace(".", dec).replace("\x00", group)


def format_distance(km: float, unit: str = "km", lang: str | None = None, decimals: int = 1) -> str:
    """Distance in the chosen unit with the language's number format and unit label."""
    from ..core.geo import KM_TO_MILES
    val = km * KM_TO_MILES if unit == "mi" else km
    return f"{format_number(val, decimals, lang)}\u00a0{tr('unit.' + ('mi' if unit == 'mi' else 'km'), lang)}"


def all_keys(lang: str) -> set:
    return set(_load(lang))
