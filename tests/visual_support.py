"""Shared helpers for visual regression (tests and scripts/update_visual_baseline.py)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
from PIL import Image

BASELINE = Path(__file__).resolve().parent / "baselines" / "phash.json"
CASES = [("light", "active"), ("neon_dark_blue", "active"), ("monochrome", "close_up"), ("high_contrast", "balanced")]
FRAMES = [15, 120, 300, 450, 590]
W, H = 640, 360


class SyntheticProvider:
    """Deterministic in-process tiles: exercises compositor + grading without network."""

    id = "synthetic-test"
    name = "Synthetic"
    max_zoom = 18
    tile_size = 256
    base_flavor = "light"

    def fetch_tile(self, z, x, y):
        from timelinerx.maps.tiles import TileResult
        yy, xx = np.mgrid[0:256, 0:256]
        land = (np.sin((xx + x * 256) / (37.0 + z)) + np.cos((yy + y * 256) / (41.0 + z)) > 0.3)
        img = np.empty((256, 256, 3), np.uint8)
        img[...] = (170, 200, 225)
        img[land] = (236, 232, 222)
        img[(xx % 64 == 0) | (yy % 64 == 0)] = (205, 205, 205)
        buf = io.BytesIO()
        Image.fromarray(img).save(buf, "PNG")
        return TileResult(True, buf.getvalue())

    def attribution(self):
        return "© synthetic test tiles"

    def rate_limit(self):
        from timelinerx.maps.tiles import RateLimitPolicy
        return RateLimitPolicy(1e6, 4)


def phash(img: Image.Image, size: int = 8) -> str:
    """64-bit DCT perceptual hash (pHash)."""
    from scipy.fft import dctn
    a = np.asarray(img.convert("L").resize((size * 4, size * 4), Image.LANCZOS), np.float64)
    d = dctn(a, norm="ortho")[:size, :size]
    med = np.median(d.flatten()[1:])
    bits = (d > med).flatten()
    return "".join("1" if b else "0" for b in bits)


def hamming(a: str, b: str) -> int:
    return sum(x != y for x, y in zip(a, b))


def render_cases(tile_root: Path):
    from timelinerx.maps.tiles import PLUGIN_PROVIDERS
    from timelinerx.pipeline.preview import PreviewSession
    from timelinerx.projects.project import Project, TimelineRef
    fix = Path(__file__).resolve().parents[1] / "fixtures" / "standard-route.json"
    PLUGIN_PROVIDERS["synthetic-test"] = SyntheticProvider
    out = {}
    for theme, mode in CASES:
        p = Project(name="VR", timeline=TimelineRef(str(fix)))
        p.visual.theme = theme
        p.visual.map_provider = "synthetic-test"
        p.camera.mode = mode
        p.title.name = "Visual"
        p.video.resolution, p.video.aspect, p.video.duration_s = "480p", "16:9", 20
        s = PreviewSession(p, "480p", allow_placeholder=False, tile_root=tile_root)
        for f in FRAMES:
            src = s.render_frame(f)
            q = src.convertToFormat(src.Format.Format_RGB888)
            arr = np.frombuffer(bytes(q.constBits()), np.uint8).reshape(q.height(), q.bytesPerLine())[:, :q.width() * 3]
            img = Image.fromarray(arr.reshape(q.height(), q.width(), 3)).resize((W, H))
            out[f"{theme}/{mode}/{f}"] = img
    return out


def load_baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.is_file() else {}
