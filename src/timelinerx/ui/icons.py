"""Stroke icons drawn from inline SVG (original, 24×24 grid, 1.75 px strokes)."""

from __future__ import annotations

from functools import lru_cache

_P = {
    "dashboard": '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
    "import": '<path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M4 17v3h16v-3"/>',
    "analysis": '<path d="M4 20V10"/><path d="M10 20V4"/><path d="M16 20v-7"/><path d="M21 20H3"/>',
    "journey": '<circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="6" r="2.5"/><path d="M8.5 17c6 0 2-10 7.5-10"/>',
    "visual": '<circle cx="12" cy="12" r="8.5"/><circle cx="8.5" cy="10" r="1.3"/><circle cx="12" cy="7.5" r="1.3"/><circle cx="15.5" cy="10" r="1.3"/><path d="M12 20.5c-1.5 0-2-1.5-1-2.5s0-3 2-3h2.5c2 0 3.5-1.5 3.5-3"/>',
    "video": '<rect x="3" y="6" width="13" height="12" rx="2"/><path d="M16 10l5-3v10l-5-3z"/>',
    "preview": '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
    "queue": '<path d="M8 6h13"/><path d="M8 12h13"/><path d="M8 18h13"/><circle cx="4" cy="6" r="1"/><circle cx="4" cy="12" r="1"/><circle cx="4" cy="18" r="1"/>',
    "library": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18"/><path d="M10 13l4 2.5-4 2.5z"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1"/>',
    "ok": '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9.5"/>',
    "warn": '<path d="M12 3.5l9.5 16.5h-19z"/><path d="M12 10v4"/><path d="M12 17.2v.1"/>',
    "error": '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 7.8v.1"/>',
    "play": '<path d="M7 5l12 7-12 7z"/>',
    "pause": '<path d="M8 5v14M16 5v14"/>',
    "folder": '<path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/>',
    "repair": '<path d="M14.5 6.5a4 4 0 00-5.2 5.2L4 17l3 3 5.3-5.3a4 4 0 005.2-5.2l-2.5 2.5-2.5-2.5z"/>',
}


@lru_cache(maxsize=256)
def icon(name: str, color: str = "#4d5561", size: int = 20):
    from PySide6.QtCore import QByteArray, Qt
    from PySide6.QtGui import QIcon, QPainter, QPixmap
    from PySide6.QtSvg import QSvgRenderer

    body = _P.get(name, _P["info"])
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
           f'stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')
    r = QSvgRenderer(QByteArray(svg.encode()))
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    r.render(p)
    p.end()
    pm.setDevicePixelRatio(2.0)
    return QIcon(pm)


_ICON_EXTRA = {
    "about": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5"/><path d="M12 7.6v.1"/>',
    "tutorial": '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M10 8.5l4.5 2.5-4.5 2.5z"/><path d="M8 21h8"/>',
    "link": '<path d="M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1"/>',
    "github": '<path d="M9 19c-4.5 1.5-4.5-2.5-6-3m12 5v-3.5c0-1 .1-1.4-.5-2 2.8-.3 5.5-1.4 5.5-6a4.6 4.6 0 00-1.3-3.2 4.3 4.3 0 00-.1-3.2s-1-.3-3.4 1.3a11.6 11.6 0 00-6 0C6.8 2.9 5.8 3.2 5.8 3.2a4.3 4.3 0 00-.1 3.2A4.6 4.6 0 004.4 9.6c0 4.6 2.7 5.7 5.5 6-.6.6-.6 1.2-.5 2V21"/>',
    "linkedin": '<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M8 10.5V17"/><path d="M8 7.3v.1"/><path d="M12 17v-6.5"/><path d="M12 13.2c0-1.6 1-2.7 2.4-2.7 1.5 0 2.1 1 2.1 2.6V17"/>',
    "plane": '<path d="M10.5 13.5L3 11l1.5-1.5 7.5 1 4-4c1-1 2.8-1.3 3.3-.8s.2 2.3-.8 3.3l-4 4 1 7.5L14 22l-2.5-7.5-3 3V20l-1.5 1-1.2-3.3L2.5 16.5 3.5 15h2.3z"/>',
}
_P.update(_ICON_EXTRA)


@lru_cache(maxsize=16)
def brand_pixmap(dark: bool, width: int, kind: str = "lockup"):
    """The TimelinerX logo (``lockup`` = mark + wordmark, ``mark`` or ``wordmark``) scaled
    for the current screen, with the surface-appropriate colour variant."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication, QPixmap

    from ..utils.paths import assets_dir
    suffix = "_dark" if dark else ("_light" if kind != "mark" else "")
    pm = QPixmap(str(assets_dir() / "brand" / f"{kind}{suffix}.png"))
    if pm.isNull():
        return QPixmap()
    app = QGuiApplication.instance()
    dpr = app.devicePixelRatio() if app is not None else 1.0
    dpr = max(1.0, float(dpr))
    out = pm.scaledToWidth(int(width * dpr), Qt.SmoothTransformation)
    out.setDevicePixelRatio(dpr)
    return out


_BRAND_IMG = {}
_BRAND_LOCK = __import__("threading").Lock()


def brand_image(dark: bool, kind: str = "lockup"):
    """Thread-safe QImage of a brand asset (QPixmap may only be used on the GUI thread; the
    tutorial exporter paints in a worker)."""
    from PySide6.QtGui import QImage

    from ..utils.paths import assets_dir
    suffix = "_dark" if dark else ("_light" if kind != "mark" else "")
    key = (kind, suffix)
    with _BRAND_LOCK:
        img = _BRAND_IMG.get(key)
        if img is None:
            img = QImage(str(assets_dir() / "brand" / f"{kind}{suffix}.png"))
            _BRAND_IMG[key] = img
        return img
