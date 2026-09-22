"""Derive every in-app brand asset from the two master logo files.

    python scripts/make_brand_assets.py

Inputs (assets/brand/source/):
    "Timeliner Logo (Only Logo).png"   mark on white
    "Timeliner Logo (With Text).png"   mark + wordmark on black

Outputs (assets/brand/, assets/icons/):
    mark.png            transparent mark, for light surfaces
    mark_dark.png       transparent mark with the navy "T" lifted for dark surfaces
    wordmark_dark.png   "TimelinerX" in white + blue X (dark surfaces)
    wordmark_light.png  "TimelinerX" in ink + blue X (light surfaces)
    lockup_dark.png / lockup_light.png   mark + wordmark side by side
    icons/app.png (512) and icons/app.ico (16…256)

Background removal is colour-to-alpha against the known flat backgrounds, so
anti-aliased edges stay clean on any surface.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "assets" / "brand" / "source"
OUT = ROOT / "assets" / "brand"
ICONS = ROOT / "assets" / "icons"
INK = np.array([15, 23, 42], float)          # slate-900, wordmark on light surfaces


def _smooth(x, a, b):
    t = np.clip((x - a) / (b - a), 0, 1)
    return t * t * (3 - 2 * t)


def unmatte(rgb: np.ndarray, bg: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """RGBA from an image over a flat background colour."""
    d = np.sqrt(((rgb - bg) ** 2).sum(-1))
    a = _smooth(d, lo, hi)
    safe = np.maximum(a, 1e-3)[..., None]
    fg = np.clip((rgb - (1 - a)[..., None] * bg) / safe, 0, 255)
    return np.dstack([fg, a * 255]).astype(np.uint8)


def trim(img: Image.Image, pad: int = 0) -> Image.Image:
    bbox = img.getchannel("A").point(lambda v: 255 if v > 6 else 0).getbbox()
    img = img.crop(bbox)
    if pad:
        c = Image.new("RGBA", (img.width + 2 * pad, img.height + 2 * pad), (0, 0, 0, 0))
        c.paste(img, (pad, pad))
        img = c
    return img


def lift_navy(rgba: np.ndarray) -> np.ndarray:
    """Dark-surface variant: the dark navy strokes of the T are lifted to a cool slate,
    keeping their gradient, so the mark stays legible on #0b1020-like backgrounds.
    The weight is continuous in brightness so there is no speckle at the boundary."""
    out = rgba.astype(float).copy()
    rgb = out[..., :3]
    mx = rgb.max(-1)
    w = 1.0 - _smooth(mx, 105.0, 150.0)          # 1 for navy (max channel < 105), 0 for the blues
    lum = rgb.mean(-1)
    t = np.clip((lum - 20) / 70, 0, 1)
    lifted = np.stack([148 + 52 * t, 163 + 52 * t, 198 + 38 * t], -1)
    out[..., :3] = rgb * (1 - w[..., None]) + lifted * w[..., None]
    return np.clip(out, 0, 255).astype(np.uint8)


def recolor_white(rgba: np.ndarray, ink: np.ndarray) -> np.ndarray:
    out = rgba.astype(float).copy()
    rgb = out[..., :3]
    mx, mn = rgb.max(-1), rgb.min(-1)
    whiteish = (mn > 150) & ((mx - mn) < 40)
    rgb[whiteish] = ink
    return out.astype(np.uint8)


def side_by_side(a: Image.Image, b: Image.Image, gap_ratio: float = 0.12) -> Image.Image:
    h = a.height
    b = b.resize((round(b.width * (h * 0.46) / b.height), round(h * 0.46)), Image.LANCZOS)
    gap = round(h * gap_ratio)
    c = Image.new("RGBA", (a.width + gap + b.width, h), (0, 0, 0, 0))
    c.paste(a, (0, 0), a)
    c.paste(b, (a.width + gap, (h - b.height) // 2), b)
    return c


def square(img: Image.Image, size: int, fill: float = 0.86) -> Image.Image:
    s = fill * size / max(img.width, img.height)
    r = img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))), Image.LANCZOS)
    c = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    c.paste(r, ((size - r.width) // 2, (size - r.height) // 2), r)
    return c


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    only = np.asarray(Image.open(SRC / "Timeliner Logo (Only Logo).png").convert("RGB"), float)
    text = np.asarray(Image.open(SRC / "Timeliner Logo (With Text).png").convert("RGB"), float)

    mark = trim(Image.fromarray(unmatte(only, np.array([255.0, 255, 255]), 6, 48)), pad=4)
    mark.save(OUT / "mark.png", optimize=True)
    mark_dark = Image.fromarray(lift_navy(np.asarray(mark)))
    mark_dark.save(OUT / "mark_dark.png", optimize=True)

    # the wordmark sits right of an empty column band (x≈529–532) between rows 575–700
    word_rgba = unmatte(text[575:700, 533:1190], np.array([0.0, 0, 0]), 6, 60)
    word_dark = trim(Image.fromarray(word_rgba), pad=2)
    word_dark.save(OUT / "wordmark_dark.png", optimize=True)
    word_light = Image.fromarray(recolor_white(np.asarray(word_dark), INK))
    word_light.save(OUT / "wordmark_light.png", optimize=True)

    side_by_side(mark_dark, word_dark).save(OUT / "lockup_dark.png", optimize=True)
    side_by_side(mark, word_light).save(OUT / "lockup_light.png", optimize=True)

    ICONS.mkdir(parents=True, exist_ok=True)
    # app icon: the dark-surface mark on a rounded navy tile (legible on light and dark taskbars);
    # at ≤32 px the speed lines are dropped so the "TX" glyph keeps enough pixels
    glyph = mark_dark.crop((int(mark_dark.width * 0.255), 0, mark_dark.width, mark_dark.height))
    def icon(size: int) -> Image.Image:
        ss = 4
        n = size * ss
        yy, xx = np.mgrid[0:n, 0:n] / n
        top, bottom = np.array([24, 33, 58.0]), np.array([9, 13, 28.0])
        bg = top * (1 - yy[..., None]) + bottom * yy[..., None]
        r = 0.22 * n
        inside = np.ones((n, n), bool)
        for cx, cy in ((r, r), (n - r, r), (r, n - r), (n - r, n - r)):
            corner = ((xx * n < r) | (xx * n > n - r)) & ((yy * n < r) | (yy * n > n - r))
            far = np.hypot(xx * n - cx, yy * n - cy) > r
            near_this = (np.abs(xx * n - cx) <= r) & (np.abs(yy * n - cy) <= r)
            inside &= ~(corner & far & near_this)
        tile = Image.fromarray(np.dstack([bg, inside * 255.0]).astype(np.uint8))
        src = glyph if size <= 32 else mark_dark
        fill = 0.80 if size <= 32 else 0.84
        g = square(src, n, fill)
        tile.alpha_composite(g)
        return tile.resize((size, size), Image.LANCZOS)
    icon(512).save(ICONS / "app.png", optimize=True)
    frames = [icon(s) for s in (16, 24, 32, 48, 64, 128, 256)]
    frames[-1].save(ICONS / "app.ico", sizes=[(f.width, f.height) for f in frames],
                    append_images=frames[:-1])
    square(mark, 1024, 0.92).save(OUT / "mark_square.png", optimize=True)
    print("brand assets written to", OUT, "and", ICONS)


if __name__ == "__main__":
    main()
