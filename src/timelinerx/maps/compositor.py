"""Map Compositor — draws graded tiles for an arbitrary viewport.

Zoom levels are cross-faded: the lower integer zoom is drawn opaque and the
next zoom is blended in as the fractional zoom rises, so tile resolution
changes never "pop" between frames (a common artefact of tile-based map
video). Tiles are graded once by the theme's node graph and kept in an LRU.
"""

from __future__ import annotations

import io
import math
import threading
from collections import OrderedDict
from typing import Iterable, List, Optional, Set, Tuple

import numpy as np
from PIL import Image

from ..core.easing import smoothstep
from ..core.errors import TileProviderError
from ..core.geo import MAX_EXTENT, WORLD_SPAN, meters_to_latlon
from ..rendering.styles import Theme, hex_rgb
from .tiles import PlainProvider, TileCache, TileKey, ideal_zoom, tiles_for_viewport

MIN_ZOOM = 1
XFADE_START = 0.25
XFADE_WIDTH = 0.5


def zoom_layers(span_x: float, width_px: int, max_zoom: int, tile_size: int = 256,
                quality_bias: float = 0.0) -> List[Tuple[int, float]]:
    """[(zoom, opacity), ...] for a viewport — at most two layers."""
    zf = ideal_zoom(span_x, width_px, tile_size, quality_bias)
    zf = max(MIN_ZOOM, min(float(max_zoom), zf))
    z0 = int(math.floor(zf))
    frac = zf - z0
    layers = [(z0, 1.0)]
    if z0 + 1 <= max_zoom:
        a = smoothstep((frac - XFADE_START) / XFADE_WIDTH)
        if a > 0.001:
            layers.append((z0 + 1, a))
    return layers


def tiles_for_plan(cx, cy, span_y, aspect: float, width_px: int, max_zoom: int,
                   tile_size: int = 256, stride: int = 1) -> Set[TileKey]:
    need: Set[TileKey] = set()
    n = len(cx)
    idx = list(range(0, n, max(1, stride)))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    for f in idx:
        sx = span_y[f] * aspect
        for z, _ in zoom_layers(sx, width_px, max_zoom, tile_size):
            need.update(tiles_for_viewport(cx[f], cy[f], sx * 1.02, span_y[f] * 1.02, z))
    return need


class MapCompositor:
    def __init__(self, cache: Optional[TileCache], theme: Theme, *, allow_placeholder: bool = False,
                 lru_tiles: int = 768, graded_dir=None):
        self.cache = cache
        self.theme = theme
        self.allow_placeholder = allow_placeholder
        self.plain = cache is None or isinstance(cache.provider, PlainProvider)
        self.max_zoom = 19 if self.plain else cache.provider.max_zoom
        self.tile_size = 256 if self.plain else cache.provider.tile_size
        self._lru: "OrderedDict[TileKey, object]" = OrderedDict()
        self._cap = lru_tiles
        self._lock = threading.Lock()
        self.placeholders_drawn = 0
        self.missing: Set[TileKey] = set()
        self.graded_dir = graded_dir

    def graded_array(self, key: TileKey) -> Optional[np.ndarray]:
        """Graded RGB uint8 tile, via the on-disk graded cache when configured."""
        from pathlib import Path
        gp = Path(self.graded_dir) / f"{key[0]}_{key[1]}_{key[2]}.png" if self.graded_dir else None
        if gp is not None and gp.is_file():
            try:
                with Image.open(gp) as im:
                    return np.asarray(im.convert("RGB"))
            except Exception:  # noqa: BLE001 — regenerate below
                pass
        data = self.cache.get_bytes(key) if self.cache else None
        if data is None and self.cache is not None:
            data, _ = self.cache.fetch(key)
        if data is None:
            return None
        try:
            with Image.open(io.BytesIO(data)) as im:
                rgb = np.asarray(im.convert("RGB"))
        except Exception as e:  # noqa: BLE001 — corrupt cache file
            raise TileProviderError(f"Corrupt tile z{key[0]}/{key[1]}/{key[2]}: {e}") from e
        graded = self.theme.grade(rgb)
        if gp is not None:
            gp.parent.mkdir(parents=True, exist_ok=True)
            tmp = gp.with_suffix(".part.png")
            Image.fromarray(graded).save(tmp, compress_level=1)
            tmp.replace(gp)
        return graded

    # -------------------------------------------------------------- tiles
    def _graded_qimage(self, key: TileKey):
        from PySide6.QtGui import QImage

        with self._lock:
            img = self._lru.get(key)
            if img is not None:
                self._lru.move_to_end(key)
                return img
        graded = self.graded_array(key)
        if graded is None:
            self.missing.add(key)
            if not self.allow_placeholder:
                raise TileProviderError(
                    f"Map tile z{key[0]}/{key[1]}/{key[2]} is unavailable "
                    f"({self.cache.failed.get(key, 'not cached') if self.cache else 'no provider'}).",
                    hint="Retry with network access, or explicitly allow placeholder tiles.")
            return None
        h, w = graded.shape[:2]
        bgra = np.empty((h, w, 4), np.uint8)
        bgra[..., 0] = graded[..., 2]
        bgra[..., 1] = graded[..., 1]
        bgra[..., 2] = graded[..., 0]
        bgra[..., 3] = 255
        qimg = QImage(bgra.data, w, h, 4 * w, QImage.Format_RGB32).copy()
        with self._lock:
            self._lru[key] = qimg
            while len(self._lru) > self._cap:
                self._lru.popitem(last=False)
        return qimg

    # --------------------------------------------------------------- draw
    def draw(self, painter, cx: float, cy: float, span_x: float, span_y: float, width: int, height: int):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QPainter

        bg = QColor(*[int(c * 255) for c in hex_rgb(self.theme.palette.background)])
        painter.fillRect(0, 0, width, height, bg)
        if self.plain:
            self._draw_graticule(painter, cx, cy, span_x, span_y, width, height)
            return
        left = cx - span_x / 2
        top = cy + span_y / 2
        sx = width / span_x
        sy = height / span_y
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        for z, opacity in zoom_layers(span_x, width, self.max_zoom, self.tile_size):
            n = 1 << z
            size = WORLD_SPAN / n
            x0 = int(math.floor((left + MAX_EXTENT) / size))
            x1 = int(math.floor((left + span_x + MAX_EXTENT) / size))
            y0 = max(0, int(math.floor((MAX_EXTENT - top) / size)))
            y1 = min(n - 1, int(math.floor((MAX_EXTENT - (top - span_y)) / size)))
            painter.setOpacity(opacity)
            for ty in range(y0, y1 + 1):
                for tx in range(x0, x1 + 1):
                    key = (z, tx % n, ty)
                    img = self._graded_qimage(key)
                    wx = -MAX_EXTENT + tx * size
                    wy = MAX_EXTENT - ty * size
                    rx = (wx - left) * sx
                    ry = (top - wy) * sy
                    rect = QRectF(rx - 0.5, ry - 0.5, size * sx + 1.0, size * sy + 1.0)
                    if img is None:
                        self.placeholders_drawn += 1
                        painter.fillRect(rect, bg)
                    else:
                        painter.drawImage(rect, img)
        painter.setOpacity(1.0)

    def _draw_graticule(self, painter, cx, cy, span_x, span_y, width, height):
        from PySide6.QtCore import QLineF
        from PySide6.QtGui import QColor, QPen

        r, g, b = [int(c * 255) for c in hex_rgb(self.theme.palette.grid[:7])]
        a = int(self.theme.palette.grid[7:9], 16) if len(self.theme.palette.grid) == 9 else 24
        pen = QPen(QColor(r, g, b, a))
        pen.setWidthF(max(1.0, min(width, height) / 900.0))
        painter.setPen(pen)
        lat_top, lon_left = meters_to_latlon(cx - span_x / 2, cy + span_y / 2)
        lat_bot, lon_right = meters_to_latlon(cx + span_x / 2, cy - span_y / 2)
        span_deg = max(1e-6, lon_right - lon_left)
        steps = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30]
        step = next((s for s in steps if span_deg / s <= 12), 30)
        left = cx - span_x / 2
        top = cy + span_y / 2
        from ..core.geo import latlon_to_meters
        lon = math.floor(lon_left / step) * step
        while lon <= lon_right + step:
            x = (latlon_to_meters(0, lon)[0] - left) * width / span_x
            painter.drawLine(QLineF(x, 0, x, height))
            lon += step
        lat = math.floor(lat_bot / step) * step
        while lat <= lat_top + step:
            if -85 < lat < 85:
                y = (top - latlon_to_meters(lat, 0)[1]) * height / span_y
                painter.drawLine(QLineF(0, y, width, y))
            lat += step
