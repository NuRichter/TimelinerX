"""Visual Style Engine — layered colour grading as a node graph (Section VI.3).

Pipeline::

    raw tile → normalize → tone_curve → color_balance → (duotone/tint)
             → [compositor draws graded tiles]
             → selective glow (trail/marker, rendered by the frame renderer)
             → vignette → grain → final composite

Map-grading nodes are *pointwise* (each output pixel depends only on the same
input pixel), so they run once per tile and the graded tile is cached; the
per-frame cost is only compositing. Post nodes (vignette, grain) run on the
final frame. Themes are JSON documents (``assets/themes/*.json`` or
``<data>/plugins/themes/*.json``) — adding a theme needs no code change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from ..core.errors import PluginError
from ..utils.paths import assets_dir, plugins_dir

Array = np.ndarray
LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)


def hex_rgb(h: str) -> Tuple[float, float, float]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore


# ------------------------------------------------------------------- nodes
def node_normalize(img: Array, p: dict) -> Array:
    contrast = float(p.get("contrast", 1.0))
    saturation = float(p.get("saturation", 1.0))
    brightness = float(p.get("brightness", 0.0))
    gamma = float(p.get("gamma", 1.0))
    if gamma != 1.0:
        img = np.power(np.clip(img, 0, 1), 1.0 / gamma)
    y = img @ LUMA
    img = y[..., None] + (img - y[..., None]) * saturation
    img = (img - 0.5) * contrast + 0.5 + brightness
    return img


def _monotone_curve(points: List[List[float]]) -> Array:
    pts = sorted((float(x), float(y)) for x, y in points)
    xs = np.array([p[0] for p in pts])
    ys = np.array([p[1] for p in pts])
    grid = np.linspace(0, 1, 1024)
    if len(xs) < 2:
        return grid
    # Fritsch–Carlson monotone cubic
    h = np.diff(xs)
    d = np.diff(ys) / np.where(h == 0, 1e-9, h)
    m = np.zeros_like(xs)
    m[0], m[-1] = d[0], d[-1]
    for i in range(1, len(xs) - 1):
        m[i] = 0.0 if d[i - 1] * d[i] <= 0 else 2.0 / (1.0 / d[i - 1] + 1.0 / d[i])
    out = np.empty_like(grid)
    idx = np.clip(np.searchsorted(xs, grid) - 1, 0, len(xs) - 2)
    t = (grid - xs[idx]) / np.where(h[idx] == 0, 1e-9, h[idx])
    t = np.clip(t, 0, 1)
    h00 = 2 * t ** 3 - 3 * t ** 2 + 1
    h10 = t ** 3 - 2 * t ** 2 + t
    h01 = -2 * t ** 3 + 3 * t ** 2
    h11 = t ** 3 - t ** 2
    out = h00 * ys[idx] + h10 * h[idx] * m[idx] + h01 * ys[idx + 1] + h11 * h[idx] * m[idx + 1]
    return np.clip(out, 0, 1)


def node_tone_curve(img: Array, p: dict) -> Array:
    lut = _monotone_curve(p.get("points", [[0, 0], [1, 1]]))
    idx = np.clip((np.clip(img, 0, 1) * 1023).astype(np.int32), 0, 1023)
    out = lut[idx]
    for ch, key in enumerate(("red", "green", "blue")):
        if key in p:
            clut = _monotone_curve(p[key])
            out[..., ch] = clut[np.clip((out[..., ch] * 1023).astype(np.int32), 0, 1023)]
    return out


def node_color_balance(img: Array, p: dict) -> Array:
    y = np.clip(img @ LUMA, 0, 1)[..., None]
    w_sh = np.clip(1.0 - y * 3.0, 0, 1)
    w_sh = w_sh * np.sqrt(w_sh)          # ^1.5 without the slow pow
    w_hi = np.clip((y - 0.66) * 3.0, 0, 1)
    w_hi = w_hi * np.sqrt(w_hi)
    w_mid = np.clip(1.0 - w_sh - w_hi, 0, 1)
    out = img.copy()
    for key, w in (("shadows", w_sh), ("midtones", w_mid), ("highlights", w_hi)):
        if key in p:
            out = out + w * np.asarray(p[key], np.float32)[None, None, :]
    if p.get("preserve_luminance", True):
        y2 = out @ LUMA
        out = out + (y[..., 0] - y2)[..., None]
    return out


def node_duotone(img: Array, p: dict) -> Array:
    dark = np.asarray(hex_rgb(p.get("dark", "#000000")), np.float32)
    light = np.asarray(hex_rgb(p.get("light", "#ffffff")), np.float32)
    amount = float(p.get("amount", 1.0))
    y = np.clip(img @ LUMA, 0, 1)[..., None]
    toned = dark + (light - dark) * y
    return img + (toned - img) * amount


def node_invert(img: Array, p: dict) -> Array:
    amount = float(p.get("amount", 1.0))
    return img + ((1.0 - img) - img) * amount


GRADING_NODES: Dict[str, Callable[[Array, dict], Array]] = {
    "normalize": node_normalize,
    "tone_curve": node_tone_curve,
    "color_balance": node_color_balance,
    "duotone": node_duotone,
    "invert": node_invert,
}
POST_NODES = ("vignette", "grain")


@dataclass
class Palette:
    background: str = "#f2eef0"
    route: str = "#e90064"
    route_glow: str = "#ff4f9a"
    trail_old: str = "#e90064"
    marker_core: str = "#24191d"
    marker_ring: str = "#e90064"
    text_primary: str = "#24191d"
    text_secondary: str = "#5c4b52"
    card_bg: str = "#fff8fae0"
    card_border: str = "#00000014"
    attribution: str = "#24191dc8"
    grid: str = "#00000012"


@dataclass
class Theme:
    id: str
    name: str
    base: str                      # light | dark — which basemap flavour it expects
    grading: List[dict]
    post: List[dict]
    palette: Palette
    glow: Dict[str, float] = field(default_factory=lambda: {"route": 0.6, "marker": 0.8})
    experimental: bool = False
    source: str = "builtin"

    def grade(self, rgb_u8: Array) -> Array:
        """Apply the map grading chain to an HxWx3 uint8 tile."""
        img = rgb_u8.astype(np.float32) / 255.0
        for node in self.grading:
            img = GRADING_NODES[node["node"]](img, node)
        return (np.clip(img, 0, 1) * 255.0 + 0.5).astype(np.uint8)

    def post_param(self, name: str) -> Optional[dict]:
        for n in self.post:
            if n.get("node") == name:
                return n
        return None


def validate_theme_dict(d: dict, source: str = "") -> List[str]:
    errs = []
    for key in ("id", "name", "base", "grading", "palette"):
        if key not in d:
            errs.append(f"missing '{key}'")
    if d.get("base") not in ("light", "dark"):
        errs.append("'base' must be 'light' or 'dark'")
    for i, n in enumerate(d.get("grading", []) or []):
        if not isinstance(n, dict) or n.get("node") not in GRADING_NODES:
            errs.append(f"grading[{i}]: unknown node {n.get('node') if isinstance(n, dict) else n!r}")
    for i, n in enumerate(d.get("post", []) or []):
        if not isinstance(n, dict) or n.get("node") not in POST_NODES:
            errs.append(f"post[{i}]: unknown node")
    pal = d.get("palette", {})
    if not isinstance(pal, dict):
        errs.append("'palette' must be an object")
    else:
        for k, v in pal.items():
            if k not in Palette.__dataclass_fields__:
                errs.append(f"palette: unknown key '{k}'")
            elif not (isinstance(v, str) and v.startswith("#") and len(v) in (4, 7, 9)):
                errs.append(f"palette.{k}: expected #rgb/#rrggbb/#rrggbbaa")
    return errs


def theme_from_dict(d: dict, source: str = "builtin") -> Theme:
    errs = validate_theme_dict(d, source)
    if errs:
        raise PluginError(f"Invalid theme {d.get('id', '?')} ({source}): " + "; ".join(errs))
    return Theme(id=d["id"], name=d["name"], base=d["base"], grading=list(d.get("grading", [])),
                 post=list(d.get("post", [])), palette=Palette(**d["palette"]),
                 glow=dict(d.get("glow", {"route": 0.6, "marker": 0.8})),
                 experimental=bool(d.get("experimental", False)), source=source)


_THEMES: Optional[Dict[str, Theme]] = None
THEME_LOAD_ERRORS: List[str] = []


def load_themes(extra_dirs: Optional[List[Path]] = None, reload: bool = False) -> Dict[str, Theme]:
    global _THEMES
    if _THEMES is not None and not reload:
        return _THEMES
    THEME_LOAD_ERRORS.clear()
    out: Dict[str, Theme] = {}
    dirs = [(assets_dir() / "themes", "builtin")]
    try:
        dirs.append((plugins_dir() / "themes", "plugin"))
    except OSError:
        pass
    for d in extra_dirs or []:
        dirs.append((Path(d), "plugin"))
    for directory, kind in dirs:
        if not directory.is_dir():
            continue
        for f in sorted(directory.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                t = theme_from_dict(data, f"{kind}:{f.name}")
                if kind == "plugin" and t.id in out and out[t.id].source.startswith("builtin"):
                    THEME_LOAD_ERRORS.append(f"{f.name}: id '{t.id}' collides with a built-in theme; skipped")
                    continue
                out[t.id] = t
            except (OSError, ValueError, PluginError) as e:
                THEME_LOAD_ERRORS.append(f"{f.name}: {e}")
    if not out:
        raise PluginError("No themes could be loaded (assets missing?).")
    _THEMES = out
    return out


def get_theme(theme_id: str) -> Theme:
    themes = load_themes()
    if theme_id not in themes:
        raise PluginError(f"Unknown theme '{theme_id}'. Available: {', '.join(sorted(themes))}")
    return themes[theme_id]


# ------------------------------------------------------------- post effects
class PostFX:
    """Vignette and film grain, precomputed once per frame size.

    Both are applied through Qt's native compositing (SourceOver for the
    vignette mask, SoftLight for grain), which is much faster than per-pixel
    numpy arithmetic. Grain uses a fixed seed and cycles 4 textures by frame
    index, so output is deterministic.
    """

    def __init__(self, theme: Theme, width: int, height: int, enabled: bool = True):
        self.vignette_img = None
        self.grain_imgs = []
        self.grain_amount = 0.0
        self.width, self.height = width, height
        if not enabled:
            return
        from PySide6.QtGui import QImage
        v = theme.post_param("vignette")
        if v and float(v.get("strength", 0)) > 0:
            yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
            nx = (xx - width / 2) / (width / 2)
            ny = (yy - height / 2) / (height / 2)
            r = np.sqrt(nx * nx * 0.85 + ny * ny)
            radius = float(v.get("radius", 0.75))
            soft = float(v.get("softness", 0.55))
            t = np.clip((r - radius) / max(1e-3, soft), 0, 1)
            t = t * t * (3 - 2 * t)
            alpha = (float(v["strength"]) * t * 255.0 + 0.5).astype(np.uint8)
            arr = np.zeros((height, width, 4), np.uint8)
            arr[..., 3] = alpha            # premultiplied black: colour channels stay 0
            self._vig_arr = arr
            self.vignette_img = QImage(arr.data, width, height, 4 * width,
                                       QImage.Format_ARGB32_Premultiplied)
        g = theme.post_param("grain")
        if g and float(g.get("amount", 0)) > 0:
            self.grain_amount = float(g["amount"])
            rng = np.random.default_rng(20240501)  # fixed seed → deterministic output
            self._grain_arrs = []
            for _ in range(4):
                n = np.clip(128 + rng.normal(0, 48, (height, width)), 0, 255).astype(np.uint8)
                arr = np.empty((height, width, 4), np.uint8)
                arr[..., 0] = n
                arr[..., 1] = n
                arr[..., 2] = n
                arr[..., 3] = 255
                self._grain_arrs.append(arr)
                self.grain_imgs.append(QImage(arr.data, width, height, 4 * width, QImage.Format_RGB32))

    def apply_qt(self, painter, frame_index: int) -> None:
        from PySide6.QtGui import QPainter
        if self.vignette_img is not None:
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.drawImage(0, 0, self.vignette_img)
        if self.grain_imgs:
            painter.save()
            painter.setCompositionMode(QPainter.CompositionMode_SoftLight)
            painter.setOpacity(min(1.0, self.grain_amount * 12.0))
            painter.drawImage(0, 0, self.grain_imgs[frame_index % len(self.grain_imgs)])
            painter.restore()
