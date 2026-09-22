"""Frame Renderer — draws one video frame from a FramePlan.

Layer order: graded map → trail shadow → full route / old trail → gradient
recent trail → selective bloom (trail + marker only) → marker (pulse ring,
glow, core) → typography (safe-area aware) → attribution → vignette/grain.

Everything is a pure function of (journey, plan, settings, frame index), so
identical inputs render identical pixels. Trail geometry is decimated in
screen space per frame (points closer than ~1.25 px are skipped), so routes
with hundreds of thousands of points stay cheap and no per-point objects are
kept alive.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from ..camera.planner import FramePlan
from ..core.easing import smootherstep, smoothstep
from ..core.geo import KM_TO_MILES
from ..i18n import is_rtl, format_date, format_distance, format_number, month_name, tr
from ..journeys.journey import Journey
from ..maps.compositor import MapCompositor
from ..projects.project import TitleSettings, TrailSettings
from ..timeline.modes import Mode

FLIGHT = int(Mode.FLIGHT)
from .qt import ensure_qt_app, families, family
from .styles import PostFX, Theme, hex_rgb

SAFE_MARGIN = 0.05       # title-safe area: 5 % inset on every edge
GRADIENT_CHUNKS = 18


def qcolor(hex_str: str, alpha_mul: float = 1.0):
    from PySide6.QtGui import QColor
    r, g, b = [int(round(c * 255)) for c in hex_rgb(hex_str[:7])]
    a = int(hex_str[7:9], 16) if len(hex_str) == 9 else 255
    return QColor(r, g, b, max(0, min(255, int(round(a * alpha_mul)))))


DEFAULT_ENDING_TEMPLATE = "{distance} · {trips} trips · {days} days"


def resolve_title(template: str, *, year: str = "", name: str = "", start: str = "", end: str = "",
                  distance: str = "", trips="0", days="0", duration: str = "") -> str:
    out = template
    for k, v in (("{year}", year), ("{name}", name.strip()), ("{start}", start), ("{end}", end),
                 ("{distance}", distance), ("{trips}", str(trips)), ("{days}", str(days)),
                 ("{duration}", duration)):
        out = out.replace(k, v)
    out = " ".join(out.split())
    out = out.strip(" ·-—|,")
    return out


@dataclass
class FrameState:
    t: float          # frame time (fractional for motion-blur sub-frames)
    cx: float
    cy: float
    span: float
    mx: float
    my: float
    d: float
    outro: float


@dataclass
class OverlayText:
    title: str
    ending_title: str
    ending_stats: str
    stats_at: Optional[object] = None      # k (0…1) → ending line with figures scaled by k


class FrameRenderer:
    def __init__(self, journey: Journey, plan: FramePlan, theme: Theme, compositor: MapCompositor,
                 trail: TrailSettings, title: TitleSettings, attribution: str, *,
                 vignette: bool = True, grain: bool = True, overlays: Optional[OverlayText] = None,
                 motion_blur: bool = False, max_subframes: int = 6):
        ensure_qt_app()
        self.motion_blur = motion_blur
        self.max_subframes = max(1, int(max_subframes))
        self.j = journey
        self.plan = plan
        self.theme = theme
        self.comp = compositor
        self.trail = trail
        self.title = title
        self.attribution = attribution
        self.W, self.H = plan.width, plan.height
        self.scale = min(self.W, self.H) / 1080.0
        self.xs, self.ys, self.cum, self.modes = densify_route(journey)
        self._flight = self.modes == FLIGHT
        total = journey.total_km
        self.recent_km = max(min(80.0, total * 0.5), total * 0.16) * max(0.05, trail.length)
        post_theme = theme
        if not vignette or not grain:
            post_theme = Theme(theme.id, theme.name, theme.base, theme.grading,
                               [p for p in theme.post if (p["node"] == "vignette" and vignette)
                                or (p["node"] == "grain" and grain)], theme.palette, theme.glow)
        self.post = PostFX(post_theme, self.W, self.H)
        self.overlays = overlays or build_overlays(journey, title)
        self._fonts()

    # ---------------------------------------------------------------- fonts
    def _fonts(self):
        from PySide6.QtGui import QFont
        ui = family("ui")
        disp = family("display")
        s = self.scale

        lang = self.title.language

        def f(fam, px, bold=False):
            q = QFont()
            q.setFamilies([fam] + [x for x in families("ui", lang)[1:] if x != fam])
            q.setPixelSize(max(9, int(round(px * s))))
            q.setBold(bold)
            q.setHintingPreference(QFont.PreferNoHinting)
            return q

        self.f_title = f(disp, 46, True)
        self.f_sub = f(ui, 28)
        self.f_small = f(ui, 17)
        self.f_end_title = f(disp, 72, True)
        self.f_end_stats = f(ui, 34)

    # ------------------------------------------------------------ transform
    def _state(self, t: float) -> "FrameState":
        """Camera/marker state at a (possibly fractional) frame time ``t``."""
        p = self.plan
        n = p.frame_count
        t = max(0.0, min(n - 1.0, t))
        i0 = int(math.floor(t))
        i1 = min(n - 1, i0 + 1)
        w = t - i0
        if w <= 1e-9:
            return FrameState(t, float(p.cx[i0]), float(p.cy[i0]), float(p.span_y[i0]), float(p.marker_x[i0]),
                              float(p.marker_y[i0]), float(p.marker_d[i0]), float(p.outro_blend[i0]))

        def L(a):
            return float(a[i0] + (a[i1] - a[i0]) * w)
        span = math.exp(math.log(p.span_y[i0]) + (math.log(p.span_y[i1]) - math.log(p.span_y[i0])) * w)
        return FrameState(t, L(p.cx), L(p.cy), span, L(p.marker_x), L(p.marker_y), L(p.marker_d),
                          L(p.outro_blend))

    def _xf(self, st: "FrameState"):
        sy = st.span
        sx = sy * self.plan.aspect
        left = st.cx - sx / 2
        top = st.cy + sy / 2
        return left, top, self.W / sx, self.H / sy

    def _to_px(self, xs, ys, xf):
        left, top, kx, ky = xf
        return (xs - left) * kx, (top - ys) * ky

    @staticmethod
    def _decimate(px: np.ndarray, py: np.ndarray, step: float) -> Tuple[np.ndarray, np.ndarray]:
        if len(px) <= 2:
            return px, py
        d = np.hypot(np.diff(px), np.diff(py))
        c = np.concatenate([[0.0], np.cumsum(d)])
        bucket = np.floor(c / step)
        keep = np.concatenate([[True], bucket[1:] != bucket[:-1]])
        keep[-1] = True
        return px[keep], py[keep]

    def _polyline(self, px, py):
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPolygonF
        return QPolygonF([QPointF(float(a), float(b)) for a, b in zip(px, py)])

    # ------------------------------------------------------------ motion blur
    def subframes(self, f: int) -> int:
        """Motion-blur sample count for frame ``f``: enough sub-frames that no layer moves more
        than ~3 px between them within a 180° shutter (half a frame). 1 when blur is off."""
        if not self.motion_blur:
            return 1
        p = self.plan
        n = p.frame_count
        a, b = max(0, f - 1), min(n - 1, f + 1)
        if b == a:
            return 1
        k = 1.0 / (b - a)
        sy = p.span_y[f]
        sx = sy * p.aspect
        cam = max(abs(p.cx[b] - p.cx[a]) * k / sx * self.W, abs(p.cy[b] - p.cy[a]) * k / sy * self.H)
        zoom = abs(math.log(p.span_y[b] / p.span_y[a])) * k * 0.5 * math.hypot(self.W, self.H)
        mk = math.hypot((p.marker_x[b] - p.marker_x[a]) * k / sx * self.W,
                        (p.marker_y[b] - p.marker_y[a]) * k / sy * self.H)
        motion = 0.5 * max(cam, zoom, mk)
        return int(max(1, min(self.max_subframes, math.ceil(motion / 3.0))))

    # --------------------------------------------------------------- render
    def render(self, f: int):
        from PySide6.QtGui import QImage, QPainter

        n_sub = self.subframes(f)
        center = self._state(float(f))
        if n_sub <= 1:
            img = QImage(self.W, self.H, QImage.Format_RGB32)
            painter = QPainter(img)
            try:
                painter.setRenderHint(QPainter.Antialiasing, True)
                bloom = self._scene(painter, center)
                self._finish(painter, f, center, bloom)
            finally:
                painter.end()
            return img
        # 180° shutter: average sub-frames spread over the middle half of the frame interval;
        # glow, post-processing and typography are applied once, at the frame's own time
        acc = None
        bloom = None
        for k in range(n_sub):
            t = f + ((k + 0.5) / n_sub - 0.5) * 0.5
            sub = QImage(self.W, self.H, QImage.Format_RGB32)
            sp = QPainter(sub)
            try:
                sp.setRenderHint(QPainter.Antialiasing, True)
                b_k = self._scene(sp, self._state(t), draw_marker=True)
            finally:
                sp.end()
            if abs(t - f) < 0.5 / n_sub + 1e-9 or bloom is None:
                bloom = b_k
            a = np.ndarray((self.H, sub.bytesPerLine() // 4, 4), np.uint8, sub.constBits())[:, :self.W, :3]
            acc = a.astype(np.float32) if acc is None else acc + a
        out = np.empty((self.H, self.W, 4), np.uint8)
        out[..., :3] = np.clip(acc / n_sub + 0.5, 0, 255).astype(np.uint8)
        out[..., 3] = 255
        img = QImage(out.data, self.W, self.H, 4 * self.W, QImage.Format_RGB32).copy()
        painter = QPainter(img)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            self._finish(painter, f, center, bloom)
        finally:
            painter.end()
        return img

    def _scene(self, painter, st: "FrameState", draw_marker: bool = True):
        """Map, routes and marker for one instant. Returns the data the glow pass needs."""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QPen
        p = self.plan
        sy = st.span
        sx = sy * p.aspect
        self.comp.draw(painter, st.cx, st.cy, sx, sy, self.W, self.H)
        xf = self._xf(st)
        d = st.d
        i = int(min(max(np.searchsorted(self.cum, d, side="right") - 1, 0), len(self.cum) - 1))
        hx, hy = self._to_px(np.array([st.mx]), np.array([st.my]), xf)
        head = (float(hx[0]), float(hy[0]))
        fade = 1.0 - smootherstep(st.outro)
        pal = self.theme.palette
        w0 = 3.2 * self.scale * self.trail.width
        w1 = 6.5 * self.scale * self.trail.width

        # full traveled route (old trail)
        if self.trail.show_full_route and fade > 0.01:
            self._draw_old_route(painter, i, head, xf, qcolor(pal.trail_old, 0.34 * fade), w0)

        # recent trail
        r0 = max(0.0, d - self.recent_km)
        k0 = int(np.searchsorted(self.cum, r0, side="left"))
        rx, ry = self._to_px(self.xs[k0:i + 1], self.ys[k0:i + 1], xf)
        rx = np.append(rx, head[0])
        ry = np.append(ry, head[1])
        rx, ry = self._decimate(rx, ry, 1.0 * max(1.0, self.scale))
        if len(rx) >= 2 and fade > 0.01:
            if self.trail.shadow:
                pen = QPen(qcolor("#000000", 0.22 * fade), w1 + 3.0 * self.scale)
                pen.setCapStyle(Qt.RoundCap)
                pen.setJoinStyle(Qt.RoundJoin)
                painter.setPen(pen)
                painter.drawPolyline(self._polyline(rx, ry + 1.6 * self.scale))
            self._draw_recent(painter, rx, ry, w1, fade)

        # outro overview of the whole route
        ob = st.outro
        if ob > 0:
            self._draw_old_route(painter, len(self.xs) - 1, None, xf,
                                 qcolor(pal.route, 0.8 * smoothstep(ob)), w0 * 1.1)
        if draw_marker and fade > 0.01:
            self._marker(painter, head, st.t, fade, d)
        return (rx, ry, head, w1, fade)

    def _finish(self, painter, f: int, st: "FrameState", bloom):
        # selective bloom on trail + marker (screen-composited, so drawing it over the marker
        # only brightens it), then grading post-FX and typography — once per output frame
        rx, ry, head, w1, fade = bloom
        if self.trail.glow and fade > 0.01 and len(rx) >= 1:
            self._bloom(painter, rx, ry, head, w1, fade)
        self.post.apply_qt(painter, f)
        self._typography(painter, f, st.d)

    def _draw_recent(self, painter, rx, ry, width, fade):
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QImage, QPainter, QPen
        pal = self.theme.palette
        n = len(rx)
        if not self.trail.gradient or n < 3:
            pen = QPen(qcolor(pal.route, fade), width)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            painter.drawPolyline(self._polyline(rx, ry))
            return
        # virtual resampling: uniform screen-space samples so long straight hops
        # still get a smooth gradient
        seg = np.hypot(np.diff(rx), np.diff(ry))
        c = np.concatenate([[0.0], np.cumsum(seg)])
        total = float(c[-1])
        if total <= 0.5:
            return
        m = max(GRADIENT_CHUNKS + 1, int(total / (3.0 * max(1.0, self.scale))))
        m = min(m, 4000)
        u = np.linspace(0.0, total, m)
        ux = np.interp(u, c, rx)
        uy = np.interp(u, c, ry)
        # draw into a transparent layer with Source composition so overlapping
        # chunk joints replace (not accumulate) alpha — no dashed banding
        if getattr(self, "_trail_layer", None) is None:
            self._trail_layer = QImage(self.W, self.H, QImage.Format_ARGB32_Premultiplied)
        layer = self._trail_layer
        layer.fill(0)
        lp = QPainter(layer)
        lp.setRenderHint(QPainter.Antialiasing, True)
        lp.setCompositionMode(QPainter.CompositionMode_Source)
        edges = np.linspace(0, m - 1, GRADIENT_CHUNKS + 1).astype(int)
        for k in range(GRADIENT_CHUNKS):
            a_idx, b_idx = edges[k], min(m, edges[k + 1] + 1)
            if b_idx - a_idx < 2:
                continue
            t = (k + 1) / GRADIENT_CHUNKS
            alpha = 0.06 + 0.94 * t ** 1.6
            pen = QPen(qcolor(pal.route, alpha * fade), width * (0.5 + 0.5 * t))
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            lp.setPen(pen)
            lp.drawPolyline(self._polyline(ux[a_idx:b_idx], uy[a_idx:b_idx]))
        lp.end()
        painter.drawImage(0, 0, layer)

    def _bloom(self, painter, rx, ry, head, width, fade):
        """Selective glow: only trail + marker are blurred (alpha channel only), tinted, and
        screen-composited, so the map itself never blooms."""
        from PySide6.QtCore import QPointF, QRectF, Qt
        from PySide6.QtGui import QImage, QPainter, QPen
        ds = 5
        w, h = max(8, self.W // ds), max(8, self.H // ds)
        mask = QImage(w, h, QImage.Format_Alpha8)
        mask.fill(0)
        lp = QPainter(mask)
        lp.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(Qt.white, max(1.0, width * 1.6 / ds))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        lp.setPen(pen)
        lp.drawPolyline(self._polyline(rx / ds, ry / ds))
        lp.setPen(Qt.NoPen)
        lp.setBrush(Qt.white)
        r = 16 * self.scale / ds
        lp.drawEllipse(QPointF(head[0] / ds, head[1] / ds), r, r)
        lp.end()
        a = np.ndarray((h, mask.bytesPerLine()), np.uint8, mask.bits())[:, :w].astype(np.float32)
        a = _box_blur(a, radius=max(2, int(4 * self.scale)), passes=3)
        strength = float(self.theme.glow.get("route", 0.6)) * fade
        a = np.clip(a * strength, 0, 255)
        cr, cg, cb = [c * 255.0 for c in hex_rgb(self.theme.palette.route_glow[:7])]
        glow = np.empty((h, w, 4), np.uint8)
        k = a / 255.0
        glow[..., 0] = (k * cb).astype(np.uint8)
        glow[..., 1] = (k * cg).astype(np.uint8)
        glow[..., 2] = (k * cr).astype(np.uint8)
        glow[..., 3] = a.astype(np.uint8)
        layer = QImage(glow.data, w, h, 4 * w, QImage.Format_ARGB32_Premultiplied)
        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode_Screen)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawImage(QRectF(0, 0, self.W, self.H), layer)
        painter.restore()

    def _draw_old_route(self, painter, i, head, xf, color, width):
        """Travelled route up to dense index ``i`` (+ the live head): ground legs solid, flight
        arcs dashed so the mode of travel stays readable in the overview."""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QPen
        tx, ty = self._to_px(self.xs[:i + 1], self.ys[:i + 1], xf)
        fl = self._flight[:i + 1]
        if head is not None:
            tx = np.append(tx, head[0])
            ty = np.append(ty, head[1])
            fl = np.append(fl, fl[-1] if len(fl) else False)
        if len(tx) < 2:
            return
        painter.setBrush(Qt.NoBrush)
        step = 1.25 * max(1.0, self.scale)
        # split into runs of equal kind; a flight run starts at the point before its first sample
        edges = np.nonzero(np.diff(fl.astype(np.int8)))[0] + 1
        bounds = [0, *edges.tolist(), len(tx)]
        for a, b in zip(bounds[:-1], bounds[1:]):
            is_f = bool(fl[a])
            a0 = max(0, a - 1)
            px, py = self._decimate(tx[a0:b], ty[a0:b], step)
            if len(px) < 2:
                continue
            pen = QPen(color, width * (0.8 if is_f else 1.0))
            pen.setCapStyle(Qt.RoundCap if not is_f else Qt.FlatCap)
            pen.setJoinStyle(Qt.RoundJoin)
            if is_f:
                pen.setDashPattern([2.4, 2.2])
            painter.setPen(pen)
            painter.drawPolyline(self._polyline(px, py))

    def _flight_blend(self, d: float) -> Tuple[float, float]:
        """(0…1 weight of the plane glyph, screen-space heading in radians) at distance d."""
        i, fr = self.j.hop_fraction(d)
        if i not in self.j.arcs:
            return 0.0, 0.0
        w = smoothstep(min(fr, 1.0 - fr) / 0.10)
        dx, dy = self.j.direction_at(d)
        return w, math.atan2(-dy * 1.0, dx * 1.0)

    def _marker(self, painter, head, f, fade, d):
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QBrush, QPen, QRadialGradient
        pal = self.theme.palette
        s = self.scale
        c = QPointF(*head)
        plane, heading = self._flight_blend(float(d))
        if plane > 0.001:
            self._plane(painter, head, heading, fade * plane)
            fade *= (1.0 - plane)
            if fade <= 0.01:
                return
        if self.trail.pulse:
            period = 1.4
            ph = ((float(f) / self.plan.fps) % period) / period
            rr = (14 + 26 * smoothstep(ph)) * s
            pen = QPen(qcolor(pal.marker_ring, 0.55 * (1 - ph) * fade), 2.2 * s)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(c, rr, rr)
        g = QRadialGradient(c, 26 * s)
        g.setColorAt(0.0, qcolor(pal.marker_ring, 0.55 * fade * min(1.0, self.theme.glow.get("marker", 0.8))))
        g.setColorAt(1.0, qcolor(pal.marker_ring, 0.0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(g))
        painter.drawEllipse(c, 26 * s, 26 * s)
        painter.setBrush(qcolor(pal.marker_core, fade))
        pen = QPen(qcolor(pal.marker_ring, fade), 3.0 * s)
        painter.setPen(pen)
        painter.drawEllipse(c, 8.5 * s, 8.5 * s)

    def _timeline_bar(self, painter, d: float, alpha: float):
        """Date ribbon: where in the period the marker is, with month (or year) ticks."""
        from PySide6.QtCore import QPointF, QRectF, Qt
        from PySide6.QtGui import QFontMetricsF
        pal = self.theme.palette
        s = self.scale
        W, H = self.W, self.H
        t0, t1 = float(self.j.t[0]), float(self.j.t[-1])
        if t1 <= t0:
            return
        i = self.j.index_at_distance(d)
        # time at the marker, interpolated inside the hop
        k, fr = self.j.hop_fraction(d)
        tn = float(self.j.t[k - 1] + (self.j.t[k] - self.j.t[k - 1]) * fr) if k > 0 else t0
        u = max(0.0, min(1.0, (tn - t0) / (t1 - t0)))
        x0 = W * SAFE_MARGIN
        x1 = W * (0.66 if self.W >= self.H else 1.0 - SAFE_MARGIN)
        y = H - H * SAFE_MARGIN * 0.62
        painter.save()
        painter.setOpacity(alpha)
        painter.setPen(Qt.NoPen)
        painter.setBrush(qcolor(pal.card_bg, 0.55))
        painter.drawRoundedRect(QRectF(x0, y - 2.5 * s, x1 - x0, 5 * s), 2.5 * s, 2.5 * s)
        painter.setBrush(qcolor(pal.route, 0.95))
        painter.drawRoundedRect(QRectF(x0, y - 2.5 * s, max(5 * s, (x1 - x0) * u), 5 * s), 2.5 * s, 2.5 * s)
        months = _month_starts(t0, t1, int(self.j.offset_min[0]))
        yearly = len(months) > 24
        painter.setFont(self.f_small)
        fm = QFontMetricsF(self.f_small, painter.device())
        last_label_x = -1e9
        for ts, yy, mm in months:
            if yearly and mm != 1:
                continue
            tx = x0 + (x1 - x0) * (ts - t0) / (t1 - t0)
            painter.setPen(Qt.NoPen)
            painter.setBrush(qcolor(pal.text_secondary, 0.8))
            painter.drawRect(QRectF(tx - 0.8 * s, y - 7 * s, 1.6 * s, 4.5 * s))
            label = str(yy) if yearly else month_name(mm, self.title.language, short=True)
            lw = fm.horizontalAdvance(label)
            if tx - lw / 2 > last_label_x + 8 * s and tx + lw / 2 < x1:
                painter.setPen(qcolor(pal.text_secondary, 0.9))
                painter.drawText(QRectF(tx - lw / 2 - 2, y - 9 * s - fm.height(), lw + 4, fm.height()),
                                 Qt.AlignCenter, label)
                last_label_x = tx + lw / 2
        hx = x0 + (x1 - x0) * u
        painter.setPen(Qt.NoPen)
        painter.setBrush(qcolor(pal.marker_ring, 0.35))
        painter.drawEllipse(QPointF(hx, y), 9 * s, 9 * s)
        painter.setBrush(qcolor(pal.marker_core))
        painter.drawEllipse(QPointF(hx, y), 5 * s, 5 * s)
        painter.restore()

    def _plane(self, painter, head, heading, alpha):
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QPainterPath, QPen, QRadialGradient, QBrush
        pal = self.theme.palette
        s = self.scale
        L = 27.0 * s
        ca, sa = math.cos(heading), math.sin(heading)
        path = QPainterPath()
        for k, (x, y) in enumerate(PLANE_OUTLINE):
            px = head[0] + (x * ca - y * sa) * L
            py = head[1] + (x * sa + y * ca) * L
            if k == 0:
                path.moveTo(px, py)
            else:
                path.lineTo(px, py)
        path.closeSubpath()
        painter.save()
        g = QRadialGradient(QPointF(*head), 30 * s)
        g.setColorAt(0.0, qcolor(pal.marker_ring, 0.45 * alpha))
        g.setColorAt(1.0, qcolor(pal.marker_ring, 0.0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(g))
        painter.drawEllipse(QPointF(*head), 30 * s, 30 * s)
        # soft drop shadow, then the glyph with a ring-coloured outline for contrast on any map
        painter.translate(0, 2.0 * s)
        painter.setBrush(qcolor("#000000", 0.28 * alpha))
        painter.drawPath(path)
        painter.translate(0, -2.0 * s)
        painter.setPen(QPen(qcolor(pal.marker_ring, alpha), 2.2 * s))
        painter.setBrush(qcolor(pal.marker_core, alpha))
        painter.drawPath(path)
        painter.restore()

    # ----------------------------------------------------------- typography
    def _date_text(self, d: float) -> str:
        dt = self.j.datetime_at_distance(d)
        return format_date(dt, self.title.language, with_day=self.title.date_format == "day")

    def _typography(self, painter, f: int, d: float):
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QFontMetricsF, QPainterPath, QPen
        pal = self.theme.palette
        W, H = self.W, self.H
        mx, my = W * SAFE_MARGIN, H * SAFE_MARGIN
        ob = float(self.plan.outro_blend[f])
        fade = 1.0 - smoothstep(ob * 1.5)
        layout = self.title.layout
        sub_parts = []
        if self.title.show_date:
            sub_parts.append(self._date_text(d))
        if self.title.show_distance:
            sub_parts.append(format_distance(d, self.title.unit, self.title.language))
        subtitle = "  •  ".join(sub_parts)
        title = self.overlays.title
        if layout != "none" and fade > 0.01 and (title or subtitle):
            dev = painter.device()           # measure on the QImage being painted, not the screen
            fm_t = QFontMetricsF(self.f_title, dev)
            fm_s = QFontMetricsF(self.f_sub, dev)
            tw = fm_t.horizontalAdvance(title) if title else 0
            sw = fm_s.horizontalAdvance(subtitle) if subtitle else 0
            pad = 22 * self.scale
            gap = 6 * self.scale
            box_w = min(W - 2 * mx, max(tw, sw) + 2 * pad + 2)
            box_h = (fm_t.height() if title else 0) + (fm_s.height() if subtitle else 0) + 2 * pad \
                + (gap if title and subtitle else 0)
            if layout == "corner":
                x, y, align = mx, my, Qt.AlignLeft
            elif layout == "centered":
                x, y, align = (W - box_w) / 2, my, Qt.AlignHCenter
            elif layout == "lower_third":
                x, y, align = mx, H - my - box_h - 26 * self.scale, Qt.AlignLeft
            else:  # minimal
                x, y, align = mx, my, Qt.AlignLeft
            if is_rtl(self.title.language) and layout != "centered":
                x, align = W - mx - box_w, Qt.AlignRight      # right-to-left scripts read from the right
            painter.save()
            painter.setOpacity(fade)
            if layout != "minimal":
                path = QPainterPath()
                path.addRoundedRect(QRectF(x, y, box_w, box_h), 16 * self.scale, 16 * self.scale)
                painter.setPen(QPen(qcolor(pal.card_border), 1.2 * self.scale))
                painter.setBrush(qcolor(pal.card_bg))
                painter.drawPath(path)
                if layout == "lower_third":
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(qcolor(pal.route))
                    painter.drawRoundedRect(QRectF(x, y, 6 * self.scale, box_h), 3 * self.scale, 3 * self.scale)
            ty = y + pad
            inner = QRectF(x + pad, ty, box_w - 2 * pad, box_h)
            if title:
                painter.setFont(self.f_title)
                painter.setPen(qcolor(pal.text_primary))
                elided = fm_t.elidedText(title, Qt.ElideRight, inner.width())
                painter.drawText(QRectF(inner.x(), ty, inner.width(), fm_t.height()), align | Qt.AlignVCenter, elided)
                ty += fm_t.height() + gap
            if subtitle:
                painter.setFont(self.f_sub)
                painter.setPen(qcolor(pal.text_secondary))
                painter.drawText(QRectF(inner.x(), ty, inner.width(), fm_s.height()), align | Qt.AlignVCenter,
                                 fm_s.elidedText(subtitle, Qt.ElideRight, inner.width()))
            painter.restore()

        # ending title card
        if self.title.ending_title and ob > 0.35:
            a = smoothstep((ob - 0.35) / 0.65)
            painter.save()
            painter.setOpacity(a)
            fm_e = QFontMetricsF(self.f_end_title, painter.device())
            fm_st = QFontMetricsF(self.f_end_stats, painter.device())
            et = self.overlays.ending_title or self.overlays.title
            st = self.overlays.ending_stats
            if getattr(self.title, "count_up", True) and self.overlays.stats_at is not None:
                # figures count up while the card settles (ease-out), final value from 90 % on
                st_final = st
                st = self.overlays.stats_at(1.0 - (1.0 - min(1.0, (ob - 0.35) / 0.55)) ** 3)
                fm_tmp = QFontMetricsF(self.f_end_stats, painter.device())
                self._end_w = max(getattr(self, "_end_w", 0.0), fm_tmp.horizontalAdvance(st_final))
            pad = 30 * self.scale
            bw = min(W - 2 * mx, max(fm_e.horizontalAdvance(et), fm_st.horizontalAdvance(st),
                                     getattr(self, "_end_w", 0.0)) + 2 * pad + 2)
            bh = fm_e.height() + fm_st.height() + 2 * pad + 8 * self.scale
            bx = (W - bw) / 2
            by = H - my - bh
            path = QPainterPath()
            path.addRoundedRect(QRectF(bx, by, bw, bh), 20 * self.scale, 20 * self.scale)
            painter.setPen(QPen(qcolor(pal.card_border), 1.2 * self.scale))
            painter.setBrush(qcolor(pal.card_bg))
            painter.drawPath(path)
            painter.setFont(self.f_end_title)
            painter.setPen(qcolor(pal.text_primary))
            painter.drawText(QRectF(bx + pad, by + pad, bw - 2 * pad, fm_e.height()), Qt.AlignHCenter,
                             fm_e.elidedText(et, Qt.ElideRight, bw - 2 * pad))
            painter.setFont(self.f_end_stats)
            painter.setPen(qcolor(pal.text_secondary))
            painter.drawText(QRectF(bx + pad, by + pad + fm_e.height() + 8 * self.scale, bw - 2 * pad,
                                    fm_st.height()), Qt.AlignHCenter,
                             fm_st.elidedText(st, Qt.ElideRight, bw - 2 * pad))
            painter.restore()

        if getattr(self.title, "timeline_bar", True) and layout != "none" and fade > 0.01:
            self._timeline_bar(painter, d, fade)

        # attribution — always present when the provider requires it
        if self.attribution:
            painter.setFont(self.f_small)
            fm = QFontMetricsF(self.f_small, painter.device())
            aw = fm.horizontalAdvance(self.attribution)
            ah = fm.height()
            ax = W - W * 0.02 - aw
            ay = H - H * 0.02 - ah
            painter.setPen(Qt.NoPen)
            painter.setBrush(qcolor(pal.card_bg, 0.75))
            painter.drawRoundedRect(QRectF(ax - 6 * self.scale, ay - 2 * self.scale, aw + 12 * self.scale,
                                           ah + 4 * self.scale), 6 * self.scale, 6 * self.scale)
            painter.setPen(qcolor(pal.attribution))
            painter.drawText(QRectF(ax, ay, aw + 1, ah), Qt.AlignLeft | Qt.AlignVCenter, self.attribution)


def _month_starts(t0: float, t1: float, offset_min: int):
    """Epoch seconds of local month starts inside (t0, t1]."""
    from datetime import datetime, timedelta, timezone
    tz = timezone(timedelta(minutes=int(offset_min)))
    a = datetime.fromtimestamp(t0, tz)
    y, m = a.year, a.month
    out = []
    while True:
        m += 1
        if m > 12:
            y, m = y + 1, 1
        ts = datetime(y, m, 1, tzinfo=tz).timestamp()
        if ts > t1:
            return out
        out.append((ts, y, m))


def densify_route(j: Journey, step_km: float = 20.0):
    """Insert virtual samples into long hops (drawing only).

    Ground hops follow the great circle between records (a straight Mercator segment would let
    long legs drift away from the marker); flight hops follow the same arc the marker flies
    along. The samples exist only in these arrays, not in the data. Returns x, y, cumulative km
    and the transport mode of the hop arriving at every sample.
    """
    from ..core.geo import WORLD_SPAN, interpolate_great_circle, latlon_to_meters
    xs, ys, cum, modes = [], [], [], []
    lats, lons, c = j.lats, j.lons, j.cum_km
    hm = j.hop_mode
    for k in range(len(lats)):
        mk = int(hm[k]) if k > 0 else 0
        if k > 0:
            seg = c[k] - c[k - 1]
            arc = j.arcs.get(k)
            if arc is not None:
                n = max(16, int(math.ceil(seg / (step_km * 0.5))))
                fx, fy = arc.at(np.arange(1, n) / n)
                for m in range(1, n):
                    xs.append(float(fx[m - 1]))
                    ys.append(float(fy[m - 1]))
                    cum.append(c[k - 1] + seg * m / n)
                    modes.append(mk)
            elif seg > step_km * 1.5:
                n = int(math.ceil(seg / step_km))
                for m in range(1, n):
                    la, lo = interpolate_great_circle(lats[k - 1], lons[k - 1], lats[k], lons[k], m / n)
                    x, y = latlon_to_meters(la, lo)
                    x += WORLD_SPAN * round((xs[-1] - x) / WORLD_SPAN)
                    xs.append(x)
                    ys.append(y)
                    cum.append(c[k - 1] + seg * m / n)
                    modes.append(mk)
        x, y = j.xs[k], j.ys[k]
        if xs:
            x += WORLD_SPAN * round((xs[-1] - x) / WORLD_SPAN)
        xs.append(x)
        ys.append(y)
        cum.append(c[k])
        modes.append(mk)
    return np.asarray(xs), np.asarray(ys), np.asarray(cum), np.asarray(modes, np.uint8)


# Airplane silhouette pointing along +x, unit half-length (original drawing).
_PLANE_TOP = [(1.00, 0.00), (0.90, 0.075), (0.42, 0.075), (0.08, 0.64), (-0.06, 0.64), (0.10, 0.075),
              (-0.55, 0.075), (-0.76, 0.31), (-0.89, 0.31), (-0.79, 0.035), (-0.93, 0.0)]
PLANE_OUTLINE = _PLANE_TOP + [(x, -y) for x, y in reversed(_PLANE_TOP[1:-1])]


def _box_blur(a: np.ndarray, radius: int, passes: int = 3) -> np.ndarray:
    """Separable box blur (3 passes ≈ Gaussian), edge-clamped."""
    out = a
    for _ in range(passes):
        for axis in (0, 1):
            pad = [(0, 0)] * out.ndim
            pad[axis] = (radius + 1, radius)
            p = np.pad(out, pad, mode="edge")
            c = np.cumsum(p, axis=axis, dtype=np.float32)
            if axis == 0:
                out = (c[2 * radius + 1:] - c[:-2 * radius - 1]) / (2 * radius + 1)
            else:
                out = (c[:, 2 * radius + 1:] - c[:, :-2 * radius - 1]) / (2 * radius + 1)
    return out


def build_overlays(j: Journey, t: TitleSettings) -> OverlayText:
    first = j.datetime_at_distance(0.0)
    last = j.datetime_at_distance(j.total_km)
    year = str(first.year) if first.year == last.year else f"{first.year}–{last.year}"
    days = (last.date() - first.date()).days + 1
    lang = t.language
    start = format_date(first, lang, short=True)
    end = format_date(last, lang, short=True)
    dist = format_distance(j.total_km, t.unit, lang)
    fmt = dict(year=year, name=t.name, start=start, end=end, distance=dist,
               trips=format_number(j.trip_count, 0, lang), days=format_number(days, 0, lang),
               duration=tr("video.duration_days", lang).format(n=days))
    title = resolve_title(t.template, **fmt) or tr("video.default_title", lang)
    ending_tpl = t.ending_template
    if ending_tpl.strip() == DEFAULT_ENDING_TEMPLATE:
        ending_tpl = tr("video.ending_template", lang)       # word order and nouns per language
    stats = resolve_title(ending_tpl, **fmt)

    def stats_at(k: float) -> str:
        k = max(0.0, min(1.0, k))
        f2 = dict(fmt, distance=format_distance(j.total_km * k, t.unit, lang, decimals=0 if k < 1 else 1),
                  trips=format_number(round(j.trip_count * k), 0, lang), days=format_number(round(days * k), 0, lang))
        return resolve_title(ending_tpl, **f2)
    return OverlayText(title=title, ending_title=title, ending_stats=stats, stats_at=stats_at)
