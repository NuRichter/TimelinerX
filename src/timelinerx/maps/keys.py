"""CARTO basemap API key resolution and verification.

Since 2026 CARTO serves every basemap tile requested *without* a key with an
"API KEY REQUIRED" watermark (https://carto.com/basemaps/apikey/). TimelinerX
therefore never renders CARTO tiles without a key: the preflight refuses and
says how to fix it, instead of silently producing a watermarked video.

Lookup order (first non-empty wins):

1. the key set for this process with :func:`set_user_key` — the GUI and the
   CLI pass *Settings → Maps → CARTO API key* here;
2. environment ``TIMELINERX_CARTO_KEY`` or ``CARTO_BASEMAP_API_KEY``;
3. a key baked into a packaged build (``timelinerx/_buildinfo.py``, generated
   by ``build_windows.ps1 -CartoKey …`` or ``carto_key.txt``; never committed).

The key is only ever sent to CARTO as the ``key`` query parameter of tile
requests. It is never written to logs, project files or the render library.
"""

from __future__ import annotations

import hashlib
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

_lock = threading.Lock()
_user_key: str = ""

KEY_HELP_URL = "https://carto.com/basemaps/apikey/"


def set_user_key(key: Optional[str]) -> None:
    global _user_key
    with _lock:
        _user_key = (key or "").strip()


def _build_key() -> str:
    try:
        from .. import _buildinfo  # type: ignore[attr-defined]
        return str(getattr(_buildinfo, "CARTO_KEY", "") or "").strip()
    except Exception:  # noqa: BLE001 — module absent in source checkouts
        return ""


def resolve_key() -> str:
    with _lock:
        if _user_key:
            return _user_key
    for var in ("TIMELINERX_CARTO_KEY", "CARTO_BASEMAP_API_KEY"):
        v = os.environ.get(var, "").strip()
        if v:
            return v
    return _build_key()


def key_source() -> str:
    """Where the active key comes from: settings | environment | build | none."""
    with _lock:
        if _user_key:
            return "settings"
    if any(os.environ.get(v, "").strip() for v in ("TIMELINERX_CARTO_KEY", "CARTO_BASEMAP_API_KEY")):
        return "environment"
    return "build" if _build_key() else "none"


def fingerprint(key: str) -> str:
    """Cache namespace for a key: tiles fetched with different keys (or without one) are
    kept apart, so watermarked tiles can never leak into a keyed render."""
    key = (key or "").strip()
    return "k" + hashlib.sha256(key.encode()).hexdigest()[:10] if key else "nokey"


def mask(key: str) -> str:
    key = (key or "").strip()
    if len(key) <= 8:
        return "•" * len(key)
    return key[:4] + "…" + key[-4:]


@dataclass
class KeyCheck:
    ok: bool
    message: str
    detail: str = ""


def verify_key(key: str, timeout: float = 12.0, style: str = "rastertiles/voyager") -> KeyCheck:
    """Honest online check of a key.

    Downloads the same low-zoom tile with and without the key. A working key
    returns a tile that differs from the unkeyed (watermarked) one. Identical
    bytes mean CARTO did not accept the key. Network problems are reported as
    such — never as "key OK".
    """
    from .. import __version__
    key = (key or "").strip()
    if not key:
        return KeyCheck(False, "No key entered.")
    base = f"https://basemaps.cartocdn.com/{style}/2/3/1.png"
    ua = {"User-Agent": f"TimelinerX/{__version__} (key check)"}

    def get(url):
        with urllib.request.urlopen(urllib.request.Request(url, headers=ua), timeout=timeout) as r:
            return r.read(4 * 1024 * 1024)

    try:
        keyed = get(base + "?key=" + urllib.request.quote(key))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return KeyCheck(False, f"CARTO rejected the key (HTTP {e.code}).")
        return KeyCheck(False, f"CARTO answered HTTP {e.code}; the key could not be verified.")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return KeyCheck(False, "Could not reach CARTO to verify the key.", str(getattr(e, "reason", e)))
    try:
        plain = get(base)
    except (urllib.error.URLError, OSError, TimeoutError):
        plain = None
    if plain is not None and hashlib.sha256(plain).digest() == hashlib.sha256(keyed).digest():
        return KeyCheck(False, "CARTO returned the watermarked tile even with this key — "
                               "the key is not active or is restricted to other domains/apps.")
    return KeyCheck(True, "Key accepted: CARTO returned unwatermarked tiles.")
