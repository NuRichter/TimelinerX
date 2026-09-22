"""Camera Planner — the "Absolute Cinema" camera (Section VI.1–VI.2, VIII.5).

Two layers:

1. **Framing layer** (adapted from Google Timeline Visualizer, (c) 2025
   mahlernim, MIT): per-sample target viewports from trip-leg-aware context
   windows, transfer framing, episode-arrival zoom and a dead-zone follower,
   plus the "visual work" pacing that spends screen time proportional to how
   much ground crosses the viewport.

2. **Cinema layer** (NuRichter): turns those targets into a camera path that
   is provably smooth:
   * zero-phase Gaussian low-pass on centre and log-scale (removes the
     velocity kinks the dead-zone follower produces, and micro-jitter),
   * Catmull-Rom (C1) sampling between track samples instead of linear
     interpolation,
   * predictive anticipation: the camera is evaluated slightly ahead of the
     marker's timeline,
   * rule-of-thirds lead room in the direction of travel (or centred framing
     for "minimal" composition),
   * eased progress ramps, eased intro/outro (smootherstep: zero velocity and
     acceleration at the ends),
   * manual Director's-Cut keyframes blended with the same easing,
   * a final per-frame low-pass and a visibility guard for the marker.

``jerk_report`` implements the Section VI.1 verification criterion.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..core.easing import (EASINGS, catmull_rom_sample, gaussian_smooth, lerp, smootherstep,
                           smoothstep, spike_ratio, zoom_pan_interpolator)
from ..core.geo import MAX_EXTENT, WORLD_SPAN, latlon_to_meters
from ..journeys.journey import (Journey, build_legs, distance_compression_timing,
                                transfer_threshold_km)
from ..timeline.modes import Mode

# Zoom styles. Keys are stored in projects; labels live in i18n ("Fixed", "Balanced", "Active",
# "Close-Up"). Parameters of fixed/balanced/active/close_up follow the upstream CameraMovement
# enum (FIXED, STEADY, DYNAMIC, CLOSE_UP) so the framing behaves like the reference app.
CAMERA_MODES: Dict[str, dict] = {
    "fixed": dict(context_fraction=0.10, minimum_context_km=25.0, maximum_context_km=350.0,
                  padding=2.6, minimum_span=0.00060, zoom_out_alpha=0.0, zoom_in_alpha=0.0,
                  leg_aware=False, fixed_zoom=True, lead=0.0, anticipation_s=0.0, smooth_s=0.9),
    "balanced": dict(context_fraction=1.00, minimum_context_km=650.0, maximum_context_km=650.0,
                     padding=2.8, minimum_span=0.00060, zoom_out_alpha=0.14, zoom_in_alpha=0.035,
                     leg_aware=False, fixed_zoom=False, lead=0.35, anticipation_s=0.25, smooth_s=1.1),
    "active": dict(context_fraction=0.10, minimum_context_km=100.0, maximum_context_km=350.0,
                   padding=2.2, minimum_span=0.00045, zoom_out_alpha=0.24, zoom_in_alpha=0.06,
                   leg_aware=True, fixed_zoom=False, lead=0.6, anticipation_s=0.30, smooth_s=0.8),
    "close_up": dict(context_fraction=0.035, minimum_context_km=6.0, maximum_context_km=120.0,
                     padding=1.7, minimum_span=0.00030, zoom_out_alpha=0.30, zoom_in_alpha=0.075,
                     leg_aware=True, fixed_zoom=False, lead=0.7, anticipation_s=0.30, smooth_s=0.7),
}
ZOOM_STYLES = ("fixed", "balanced", "active", "close_up")
# projects written by 0.x used the five-mode names
LEGACY_CAMERA_MODES = {"steady": "balanced", "dynamic": "active"}

# Long-trip pacing: how much screen time long trips (flights, long drives) get relative to the
# ground they cover on screen. "natural" = proportional; the others shorten long trips so
# local episodes get more time. Distance pacing keeps the upstream exponents.
LONG_TRIP_PACING = {"natural": 1.00, "balanced": 0.72, "faster": 0.48, "fastest": 0.30}
LONG_TRIP_EXPONENTS = {"natural": 1.00, "balanced": 0.85, "faster": 0.75, "fastest": 0.65}
LEGACY_PACING = {"off": "natural", "gentle": "balanced", "strong": "faster", "stronger": "fastest"}
MIN_LONG_TRIP_S = 0.45          # a long trip is never shorter than this on screen (no jump cuts)
COMFORT_SPEED_VP_S = 0.30       # comfortable marker pace, viewports per second
BRISK_SPEED_VP_S = 0.55

LOCAL_FRAMING = {"off": (False, 1.00), "balanced": (True, 1.00), "close": (True, 0.78)}
TRANSFER_PADDING = 2.8
DEAD_ZONE_HALF = 0.20
FIXED_ZOOM_PERCENTILE = 0.80
VISUAL_ZOOM_WORK_WEIGHT = 0.35
EPISODE_ARRIVAL_ZOOM_START = 0.65
MIN_CONTEXT_KM = 15.0
MAX_SPAN = 0.72 * WORLD_SPAN


@dataclass
class Keyframe:
    """Director's-Cut manual camera keyframe."""

    time_s: float
    lat: float
    lon: float
    span_km: float           # vertical ground span shown at that latitude
    hold_s: float = 1.0
    ramp_s: float = 1.2
    ease: str = "smootherstep"

    @classmethod
    def from_dict(cls, d: dict) -> "Keyframe":
        return cls(**{k: d[k] for k in ("time_s", "lat", "lon", "span_km") },
                   hold_s=d.get("hold_s", 1.0), ramp_s=d.get("ramp_s", 1.2),
                   ease=d.get("ease", "smootherstep"))


@dataclass
class CameraConfig:
    mode: str = "active"             # zoom style: fixed | balanced | active | close_up
    composition: str = "thirds"      # thirds | centered
    local_framing: str = "balanced"  # off | balanced | close
    pacing: str = "visual_zoom"      # visual_zoom | visual | distance
    compression: str = "balanced"    # long-trip pacing: natural | balanced | faster | fastest
    trip_detection: str = "balanced"
    smoothing: float = 1.0           # multiplier on the mode's smoothing time
    anticipation: float = 1.0        # multiplier on the mode's anticipation
    intro_s: float = 1.2
    outro_transition_s: float = 1.2
    outro_hold_s: float = 1.0
    progress_ramp: float = 0.05      # fraction of journey time used to ease in/out
    keyframes: List[Keyframe] = field(default_factory=list)
    overview_bottom_reserve: float = 0.0     # fraction of the frame kept free for the ending card
    outro_start_frame: Optional[int] = None   # set by audio beat sync

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class FramePlan:
    fps: int
    width: int
    height: int
    cx: np.ndarray
    cy: np.ndarray
    span_y: np.ndarray
    marker_x: np.ndarray
    marker_y: np.ndarray
    marker_d: np.ndarray
    phase: np.ndarray            # 0 intro, 1 journey, 2 outro
    outro_blend: np.ndarray      # 0..1 (for overlay fading)
    intro_blend: np.ndarray      # 1 at first frame → 0 when journey starts
    overview: Tuple[float, float, float]

    @property
    def aspect(self) -> float:
        return self.width / self.height

    @property
    def frame_count(self) -> int:
        return int(self.cx.shape[0])

    def save(self, path) -> None:
        np.savez(path, fps=self.fps, width=self.width, height=self.height, cx=self.cx, cy=self.cy,
                 span_y=self.span_y, marker_x=self.marker_x, marker_y=self.marker_y,
                 marker_d=self.marker_d, phase=self.phase, outro_blend=self.outro_blend,
                 intro_blend=self.intro_blend, overview=np.asarray(self.overview))

    @classmethod
    def load(cls, path) -> "FramePlan":
        with np.load(path) as d:
            return cls(int(d["fps"]), int(d["width"]), int(d["height"]), d["cx"], d["cy"],
                       d["span_y"], d["marker_x"], d["marker_y"], d["marker_d"], d["phase"],
                       d["outro_blend"], d["intro_blend"], tuple(d["overview"].tolist()))


# ------------------------------------------------------------ geometry helpers
class RouteGeometry:
    """Unwrapped projected route with distance lookup."""

    def __init__(self, j: Journey):
        self.j = j
        self.cum = j.cum_km
        self.xs = np.asarray(j.xs)
        self.ys = np.asarray(j.ys)

    def xy_at(self, d: float) -> Tuple[float, float]:
        return self.j.xy_at(d)

    def legs(self, trip_detection: str):
        flights = np.asarray(self.j.hop_mode) == Mode.FLIGHT
        return build_legs(self.cum, transfer_threshold_km(self.cum, trip_detection), flights)


def _raw_sample(geo: RouteGeometry, d: float, mode: dict, legs, aspect: float,
                framing: Tuple[bool, float]) -> Tuple[float, float, float]:
    cum = geo.cum
    total = cum[-1]
    cx, cy = geo.xy_at(d)
    prop_ctx = max(mode["minimum_context_km"], min(mode["maximum_context_km"], total * mode["context_fraction"]))
    leg = None
    if mode["leg_aware"] and legs:
        starts = [lg[0] for lg in legs]
        k = bisect.bisect_right(starts, max(0.0, min(total, d))) - 1
        k = max(0, min(k, len(legs) - 1))
        leg = legs[k]
        nxt = legs[k + 1] if (k < len(legs) - 1 and not legs[k + 1][2]) else None
    else:
        nxt = None
    enabled, pad_mult = framing
    blend = 0.0
    if enabled and leg is not None and leg[2] and nxt is not None:
        ll = leg[1] - leg[0]
        if ll > 0:
            frac = (d - leg[0]) / ll
            blend = smoothstep((frac - EPISODE_ARRIVAL_ZOOM_START) / (1 - EPISODE_ARRIVAL_ZOOM_START))
    if leg is not None and leg[2]:
        ll = leg[1] - leg[0]
        if blend > 0:
            arr = max(MIN_CONTEXT_KM, min(prop_ctx, nxt[1] - nxt[0]))
            ctx = math.exp(lerp(math.log(max(MIN_CONTEXT_KM, ll)), math.log(arr), blend))
            padding = lerp(TRANSFER_PADDING, mode["padding"] * pad_mult, blend)
        else:
            ctx, padding = ll, TRANSFER_PADDING
        r0, look = leg[0], leg[1]
    else:
        padding = mode["padding"] * (pad_mult if enabled else 1.0)
        r0 = leg[0] if leg is not None else 0.0
        look = total
        ctx = prop_ctx
    tail = max(r0, d - ctx)
    ahead = min(look, d + ctx)
    i0 = bisect.bisect_left(cum, tail)
    i1 = bisect.bisect_right(cum, ahead)
    fx = list(geo.xs[i0:i1])
    fy = list(geo.ys[i0:i1])
    for e in (tail, d, ahead):
        ex, ey = geo.xy_at(e)
        fx.append(ex)
        fy.append(ey)
    min_span = mode["minimum_span"] * WORLD_SPAN
    sx = max(fx) - min(fx)
    sy = max(fy) - min(fy)
    span_y = max(sy * padding, (sx * padding) / max(0.1, aspect), min_span)
    return cx, cy, min(span_y, MAX_SPAN)


def build_track(geo: RouteGeometry, mode_name: str, distance_at: Callable[[float], float],
                samples: int, aspect: float, trip_detection: str, local_framing: str
                ) -> np.ndarray:
    """(samples+1, 3) array of (cx, cy, span_y) after the upstream dead-zone follower."""
    mode = CAMERA_MODES[mode_name]
    legs = geo.legs(trip_detection)
    framing = LOCAL_FRAMING.get(local_framing, LOCAL_FRAMING["balanced"])
    raw = [_raw_sample(geo, distance_at(s / samples), mode, legs, aspect, framing)
           for s in range(samples + 1)]
    fixed = None
    if mode["fixed_zoom"]:
        spans = sorted(r[2] for r in raw)
        fixed = spans[int((len(spans) - 1) * FIXED_ZOOM_PERCENTILE)]
    track = []
    for rx, ry, rs in raw:
        target = fixed if fixed is not None else rs
        if not track:
            track.append((rx, ry, target))
            continue
        cx, cy, prev = track[-1]
        alpha = mode["zoom_out_alpha"] if target > prev else mode["zoom_in_alpha"]
        span = target if mode["fixed_zoom"] else math.exp(math.log(prev) + (math.log(target) - math.log(prev)) * alpha)
        dx, dy = span * aspect * DEAD_ZONE_HALF, span * DEAD_ZONE_HALF
        if rx < cx - dx:
            cx = rx + dx
        elif rx > cx + dx:
            cx = rx - dx
        if ry < cy - dy:
            cy = ry + dy
        elif ry > cy + dy:
            cy = ry - dy
        track.append((cx, cy, span))
    return np.asarray(track)


def _hermite_map(xv: np.ndarray, yv: np.ndarray) -> Callable[[float], float]:
    """Monotone cubic (Fritsch–Carlson style) map through strictly increasing samples."""
    from ..journeys.journey import _monotone_slopes
    xl, yl = xv.tolist(), yv.tolist()
    sl = _monotone_slopes(xl, yl)

    def f(p: float) -> float:
        e = max(0.0, min(1.0, p))
        ti = min(max(bisect.bisect_left(xl, e), 1), len(xl) - 1)
        fi = ti - 1
        w = xl[ti] - xl[fi]
        t = 0.0 if w <= 0 else (e - xl[fi]) / w
        t2, t3 = t * t, t * t * t
        return ((2 * t3 - 3 * t2 + 1) * yl[fi] + (t3 - 2 * t2 + t) * w * sl[fi]
                + (-2 * t3 + 3 * t2) * yl[ti] + (t3 - t2) * w * sl[ti])

    return f


@dataclass
class VisualWork:
    distances: np.ndarray        # sample distances (km), strictly increasing, 0 … total
    work: np.ndarray             # per-interval visual work (viewport units), len = len(distances)-1
    transfer: np.ndarray         # per-interval: inside a long trip
    legs: list
    grid: np.ndarray             # distance grid of the target-span profile
    logspan: np.ndarray          # log target span (projected metres) on ``grid``

    @property
    def total(self) -> float:
        return float(self.work.sum())


def visual_work(geo: RouteGeometry, mode_name: str, aspect: float, trip_detection: str,
                local_framing: str, include_zoom: bool, samples: int = 480) -> Optional[VisualWork]:
    """How much the picture changes along the route.

    Work between two nearby route positions = marker displacement measured in viewports of the
    framing at that point (+ optional zoom work). Samples are taken at every route vertex (not
    only on a uniform distance grid), so zig-zag local trips — many short hops that go out and
    come back — are counted in full instead of being averaged away. This is what keeps busy local
    episodes from racing across the screen.
    """
    total = geo.cum[-1] if geo.cum else 0.0
    if len(geo.cum) < 2 or total <= 0:
        return None
    # Work is measured against the *target* framing at each place (what the camera is asked to
    # show there), not against a follower track: the follower's lag depends on the timing being
    # computed, which would make the result circular. The target span profile is lightly
    # smoothed along the route so framing switches do not create work spikes.
    mode = CAMERA_MODES[mode_name]
    legs0 = geo.legs(trip_detection)
    framing = LOCAL_FRAMING.get(local_framing, LOCAL_FRAMING["balanced"])
    grid = np.linspace(0.0, total, samples + 1)
    raw = np.asarray([_raw_sample(geo, float(d), mode, legs0, aspect, framing)[2] for d in grid])
    if mode["fixed_zoom"]:
        raw[:] = np.sort(raw)[int((len(raw) - 1) * FIXED_ZOOM_PERCENTILE)]
    logspan = np.asarray(gaussian_smooth(np.log(np.maximum(1.0, raw)).tolist(), 2.0))
    verts = np.asarray(geo.cum, float)
    if len(verts) > 12000:
        verts = verts[np.linspace(0, len(verts) - 1, 12000).astype(int)]
    extra = []
    for i in geo.j.arcs:
        a, b = geo.cum[i - 1], geo.cum[i]
        extra.extend(a + (b - a) * k / 12.0 for k in range(1, 12))
    D = np.unique(np.concatenate([grid, verts, np.asarray(extra, float)]))
    D = D[(D >= 0) & (D <= total)]
    # drop near-duplicates (numerical) so every interval has positive length
    keep = np.concatenate([[True], np.diff(D) > 1e-9])
    D = D[keep]
    pts = np.asarray([geo.xy_at(float(d)) for d in D])
    ls = np.interp(D, grid, logspan)
    sy = np.exp(0.5 * (ls[:-1] + ls[1:]))
    sx = sy * aspect
    g = np.hypot(np.diff(pts[:, 0]) / sx, np.diff(pts[:, 1]) / sy)
    z = VISUAL_ZOOM_WORK_WEIGHT * np.abs(np.diff(ls)) / math.log(2) if include_zoom else 0.0
    w = g + z
    pos = w[np.isfinite(w) & (w > 1e-12)]
    if not len(pos):
        return None
    med = float(np.median(pos))
    # like the reference app: no single interval may dominate or vanish completely
    w = np.clip(np.where(np.isfinite(w), w, med), med * 0.05, med * 20.0)
    legs = geo.legs(trip_detection)
    mid = 0.5 * (D[:-1] + D[1:])
    transfer = np.zeros(len(w), bool)
    for a, b, is_t in legs:
        if is_t:
            transfer |= (mid >= a) & (mid <= b)
    return VisualWork(D, w, transfer, legs, grid, logspan)


def visual_timing(geo: RouteGeometry, mode_name: str, aspect: float, trip_detection: str,
                  local_framing: str, include_zoom: bool, samples: int = 480,
                  long_trip_pacing: str = "balanced", journey_s: Optional[float] = None
                  ) -> Callable[[float], float]:
    """Progress (0…1 of the journey's screen time) → route distance, spending time in
    proportion to visual work, with long trips weighted by ``long_trip_pacing``."""
    total = geo.cum[-1] if geo.cum else 0.0
    linear = lambda p: total * max(0.0, min(1.0, p))  # noqa: E731
    return _timing_from_work(geo, mode_name, aspect, trip_detection, local_framing, include_zoom,
                             samples, long_trip_pacing, journey_s, None)


def _timing_from_work(geo, mode_name, aspect, trip_detection, local_framing, include_zoom, samples,
                      long_trip_pacing, journey_s, prev) -> Callable[[float], float]:
    total = geo.cum[-1] if geo.cum else 0.0
    linear = lambda p: total * max(0.0, min(1.0, p))  # noqa: E731
    vw = visual_work(geo, mode_name, aspect, trip_detection, local_framing, include_zoom, samples)
    if vw is None:
        return linear
    pacing = LEGACY_PACING.get(long_trip_pacing, long_trip_pacing)
    w = vw.work * np.where(vw.transfer, LONG_TRIP_PACING.get(pacing, 0.72), 1.0)
    tw = float(w.sum())
    if tw <= 0:
        return linear
    share = w / tw
    # Minimum on-screen time per long trip: enough for the camera to pull out to the trip's
    # framing and settle back in (≈0.26 s per zoom doubling each way + 0.3 s), scaled by the
    # pacing choice. Together long trips never take more than half of the journey's time.
    trips = [(a, b) for a, b, t in vw.legs if t]
    if journey_s and trips:
        mid = 0.5 * (vw.distances[:-1] + vw.distances[1:])
        scale = {"natural": 1.25, "balanced": 1.0, "faster": 0.8, "fastest": 0.6}.get(pacing, 1.0)
        wants = []
        for a, b in trips:
            m = (mid >= a) & (mid <= b)
            if not m.any():
                continue
            ls_in = float(np.interp(0.5 * (a + b), vw.grid, vw.logspan))
            ls_a = float(np.interp(max(0.0, a - 1e-6), vw.grid, vw.logspan))
            ls_b = float(np.interp(min(total, b + 1e-6), vw.grid, vw.logspan))
            dbl = (max(0.0, ls_in - ls_a) + max(0.0, ls_in - ls_b)) / math.log(2)
            need_s = max(MIN_LONG_TRIP_S, 0.3 + 0.26 * dbl) * scale
            wants.append((m, need_s / journey_s))
        tot = sum(wv for _, wv in wants)
        if tot > 0.5:
            wants = [(m, wv * 0.5 / tot) for m, wv in wants]
        extra_total = 0.0
        grow = []
        for m, want in wants:
            have = float(share[m].sum())
            if have < want:
                grow.append((m, have, want))
                extra_total += want - have
        if grow:
            donors = np.ones(len(share), bool)
            for m, _, _ in grow:
                donors &= ~m
            dsum = float(share[donors].sum())
            if dsum > extra_total > 0:
                share[donors] *= (dsum - extra_total) / dsum
                for m, have, want in grow:
                    if have > 0:
                        share[m] *= want / have
                    else:
                        share[m] = want / m.sum()
    el = np.concatenate([[0.0], np.cumsum(share)])
    el /= el[-1]
    # strictly increasing abscissae for the monotone map
    keep = np.concatenate([[True], np.diff(el) > 1e-12])
    xv, yv = el[keep], vw.distances[keep]
    xv[-1], yv[-1] = 1.0, total
    if len(xv) < 3:
        return linear
    return _hermite_map(xv, yv)


def screen_travel(plan: "FramePlan", journey: Optional[Journey] = None, trip_detection: str = "balanced",
                  long_trip_weight: float = 1.0) -> float:
    """Total marker travel during the journey phase, in viewports (x in widths, y in heights),
    plus zoom work — what the viewer actually has to follow."""
    jf = np.nonzero(plan.phase == 1)[0]
    if len(jf) < 3:
        return 0.0
    sy = plan.span_y[jf]
    vx = np.diff(plan.marker_x[jf]) / (sy[1:] * plan.aspect)
    vy = np.diff(plan.marker_y[jf]) / sy[1:]
    tr = np.hypot(vx, vy) + VISUAL_ZOOM_WORK_WEIGHT * np.abs(np.diff(np.log2(sy)))
    if journey is not None and long_trip_weight != 1.0:
        md = plan.marker_d[jf]
        midd = 0.5 * (md[1:] + md[:-1])
        cum = journey.cum_km
        flights = np.asarray(journey.hop_mode) == Mode.FLIGHT
        for a, b, t in build_legs(cum, transfer_threshold_km(cum, trip_detection), flights):
            if t:
                m = (midd >= a) & (midd <= b)
                tr[m] *= long_trip_weight
    return float(tr.sum())


def recommend_duration(journey: Journey, cfg: "CameraConfig", width: int, height: int) -> Dict[str, float]:
    """Video lengths at which the marker moves at a comfortable / brisk on-screen pace for this
    journey, zoom style and pacing. Measured on an actual (low frame-rate) camera plan, so it
    includes the camera's real zoom behaviour. An estimate — the UI labels it as such."""
    fixed = max(0.0, cfg.intro_s) + max(0.0, cfg.outro_transition_s) + max(0.0, cfg.outro_hold_s)
    if len(journey.cum_km) < 2 or journey.total_km <= 0:
        return {"comfortable_s": max(10.0, fixed + 8.0), "brisk_s": max(8.0, fixed + 5.0), "travel_vp": 0.0}
    probe = CameraConfig(**{**cfg.__dict__, "keyframes": [], "outro_start_frame": None})
    ref_s = 60.0
    plan = plan_frames(journey, probe, width=width, height=height, fps=12, duration_s=ref_s)
    tr = screen_travel(plan)
    comfy = fixed + tr / COMFORT_SPEED_VP_S
    brisk = fixed + tr / BRISK_SPEED_VP_S
    return {"comfortable_s": float(min(1800.0, max(10.0, comfy))),
            "brisk_s": float(min(1800.0, max(8.0, brisk))), "travel_vp": tr}


def pace_of(plan: "FramePlan") -> float:
    """Average marker pace of a plan in viewports per second (journey phase)."""
    jf = int((plan.phase == 1).sum())
    return screen_travel(plan) / max(1e-6, jf / plan.fps)


def overview_viewport(xs: Sequence[float], ys: Sequence[float], aspect: float) -> Tuple[float, float, float]:
    mnx, mxx, mny, mxy = min(xs), max(xs), min(ys), max(ys)
    sx = max(mxx - mnx, 1000.0)
    sy = max(mxy - mny, 1000.0)
    span_y = max(sy * 1.35, sx * 1.35 / max(0.1, aspect))
    return (mnx + mxx) / 2, (mny + mxy) / 2, min(span_y, MAX_SPAN * 1.2)


def ramped_progress(u: float, ramp: float) -> float:
    """Trapezoidal velocity profile: smooth acceleration over ``ramp`` at each end.

    Maps [0,1]→[0,1] with zero velocity at both ends and constant velocity in
    the middle; the first derivative is continuous everywhere.
    """
    u = max(0.0, min(1.0, u))
    r = max(1e-6, min(0.45, ramp))
    vmax = 1.0 / (1.0 - r)       # area under the profile = 1

    def area(x: float) -> float:
        # ∫0^x v(s) ds where v ramps by smoothstep over [0,r]
        if x <= r:
            t = x / r
            # ∫ smoothstep = r * (t^3 - t^4/2)
            return vmax * r * (t ** 3 - 0.5 * t ** 4)
        if x <= 1 - r:
            return vmax * (0.5 * r + (x - r))
        return 1.0 - area(1.0 - x)

    return max(0.0, min(1.0, area(u)))


# ------------------------------------------------------------------ planner
def plan_frames(journey: Journey, cfg: CameraConfig, *, width: int, height: int, fps: int,
                duration_s: float) -> FramePlan:
    mode_name = LEGACY_CAMERA_MODES.get(cfg.mode, cfg.mode)
    if mode_name not in CAMERA_MODES:
        raise ValueError(f"Unknown zoom style {cfg.mode!r}")
    mode = CAMERA_MODES[mode_name]
    aspect = width / height
    geo = RouteGeometry(journey)
    total_frames = max(2, int(round(duration_s * fps)))
    # Intro/outro fly between the overview and the first/last close framing. Their length grows
    # with the zoom distance (≈0.3 s per doubling) so a big zoom never becomes a lurch; each is
    # capped at 18 % of the video. A user setting of 0 (no intro/outro) is respected.
    ov0 = overview_viewport(journey.xs, journey.ys, aspect)
    intro_s, outro_tr_s = cfg.intro_s, cfg.outro_transition_s
    if geo.cum and geo.cum[-1] > 0:
        legs = geo.legs(cfg.trip_detection)
        fr = LOCAL_FRAMING.get(cfg.local_framing, LOCAL_FRAMING["balanced"])
        s0 = _raw_sample(geo, 0.0, mode, legs, aspect, fr)[2]
        s1 = _raw_sample(geo, geo.cum[-1], mode, legs, aspect, fr)[2]
        cap = 0.18 * duration_s
        if intro_s > 0:
            intro_s = max(intro_s, min(cap, 0.55 + 0.30 * abs(math.log2(ov0[2] / max(1.0, s0)))))
        if outro_tr_s > 0:
            outro_tr_s = max(outro_tr_s, min(cap, 0.55 + 0.30 * abs(math.log2(ov0[2] / max(1.0, s1)))))
    intro_f = int(round(max(0.0, intro_s) * fps))
    outro_f = int(round((max(0.0, outro_tr_s) + max(0.0, cfg.outro_hold_s)) * fps))
    if cfg.outro_start_frame is not None:
        outro_f = max(int(0.5 * fps), total_frames - int(cfg.outro_start_frame))
    min_journey = max(2, int(0.4 * total_frames))
    if intro_f + outro_f > total_frames - min_journey:
        scale = (total_frames - min_journey) / max(1, intro_f + outro_f)
        intro_f, outro_f = int(intro_f * scale), int(outro_f * scale)
    journey_f = total_frames - intro_f - outro_f
    journey_s = journey_f / fps

    # pacing
    pacing = LEGACY_PACING.get(cfg.compression, cfg.compression)
    if cfg.pacing == "distance":
        distance_at = distance_compression_timing(journey.cum_km, pacing)
    else:
        distance_at = visual_timing(geo, mode_name, aspect, cfg.trip_detection, cfg.local_framing,
                                    include_zoom=cfg.pacing == "visual_zoom", long_trip_pacing=pacing,
                                    journey_s=journey_s)

    samples = int(min(4000, max(480, journey_f // 2)))
    smooth_s = mode["smooth_s"] * max(0.0, cfg.smoothing)
    antic = mode["anticipation_s"] * max(0.0, cfg.anticipation) / max(1e-6, journey_s)
    n = total_frames
    phase = np.zeros(n, np.uint8)
    uu = np.zeros(n)
    for f in range(n):
        if f < intro_f:
            phase[f], uu[f] = 0, 0.0
        elif f < intro_f + journey_f:
            phase[f], uu[f] = 1, (f - intro_f) / max(1, journey_f - 1)
        else:
            phase[f], uu[f] = 2, 1.0
    prog = np.asarray([ramped_progress(u, cfg.progress_ramp) for u in uu])

    def journey_pass(distance_at):
        """Camera for one timing: follower track → viewport-space low-pass → per-frame
        Catmull-Rom sampling around a lightly smoothed marker anchor."""
        track = build_track(geo, mode_name, distance_at, samples, aspect, cfg.trip_detection,
                            cfg.local_framing)
        # zero-phase low-pass in *viewport space*: the camera is expressed as the marker
        # position plus an offset measured in spans, so smoothing never drags the view
        # away from the marker while the scale changes by orders of magnitude
        sigma = smooth_s / max(1e-6, journey_s) * samples
        ms = np.asarray([geo.xy_at(distance_at(k / samples)) for k in range(samples + 1)])
        span_t = track[:, 2]
        offx = (track[:, 0] - ms[:, 0]) / (span_t * aspect)
        offy = (track[:, 1] - ms[:, 1]) / span_t
        ls = np.asarray(gaussian_smooth(np.log(span_t).tolist(), sigma))
        offx_s = np.asarray(gaussian_smooth(offx.tolist(), sigma))
        offy_s = np.asarray(gaussian_smooth(offy.tolist(), sigma))
        md = np.asarray([distance_at(p) for p in prog])
        xy = np.asarray([geo.xy_at(d) for d in md])
        mx, my = xy[:, 0].copy(), xy[:, 1].copy()
        pcs = np.minimum(1.0, prog + antic * (1.0 - prog))   # anticipation → 0 at the end (C1)
        # lightly smoothed marker path (rounds route corners over ~0.2 s) as the camera anchor
        sig_m = max(0.5, 0.2 * fps)
        ax = np.asarray(gaussian_smooth(mx.tolist(), sig_m))
        ay = np.asarray(gaussian_smooth(my.tolist(), sig_m))
        sy = np.zeros(n)
        cx = np.zeros(n)
        cy = np.zeros(n)
        for f in range(n):
            pos = pcs[f] * samples
            sy[f] = math.exp(catmull_rom_sample(ls, pos))
            cx[f] = ax[f] + catmull_rom_sample(offx_s, pos) * sy[f] * aspect
            cy[f] = ay[f] + catmull_rom_sample(offy_s, pos) * sy[f]
        return md, mx, my, ax, ay, cx, cy, sy

    md, mx, my, ax, ay, cx, cy, sy = journey_pass(distance_at)

    # Screen-speed equalisation. The pacing above estimates visual work from the *target*
    # framing; the real camera lags behind it (zoom follower + low-pass), most visibly on dense
    # timelines where trips are short on screen. Two damped passes measure what the viewer
    # actually sees (marker travel in viewports + zoom) and re-time the journey so that travel
    # is spread evenly, still weighting long trips by the long-trip pacing.
    if cfg.pacing != "distance" and journey_f > 8 and geo.cum and geo.cum[-1] > 0:
        trip_w = LONG_TRIP_PACING.get(pacing, 0.72)
        legs_t = [(a, b) for a, b, t in geo.legs(cfg.trip_detection) if t]
        jf = np.nonzero(phase == 1)[0]
        for _ in range(2):
            vx = np.diff(mx[jf]) / (sy[jf][1:] * aspect)
            vy = np.diff(my[jf]) / sy[jf][1:]
            travel = np.hypot(vx, vy) + VISUAL_ZOOM_WORK_WEIGHT * np.abs(np.diff(np.log2(sy[jf])))
            midd = 0.5 * (md[jf][1:] + md[jf][:-1])
            if legs_t and trip_w != 1.0:
                inside = np.zeros(len(travel), bool)
                for a0, b0 in legs_t:
                    inside |= (midd >= a0) & (midd <= b0)
                travel = np.where(inside, travel * trip_w, travel)
            tot = float(travel.sum())
            if tot <= 0:
                break
            pj = prog[jf]
            T = np.concatenate([[0.0], np.cumsum(travel)]) / tot
            U = (pj - pj[0]) / max(1e-12, pj[-1] - pj[0])
            Td = 0.5 * T + 0.5 * U                    # damped: move halfway toward uniform
            Td = np.maximum.accumulate(Td + np.arange(len(Td)) * 1e-9)
            Td = (Td - Td[0]) / (Td[-1] - Td[0])
            old = distance_at
            p0, p1 = float(pj[0]), float(pj[-1])

            def retimed(p, old=old, Td=Td, pj=pj, p0=p0, p1=p1):
                if p <= p0 or p >= p1:
                    return old(p)
                q = float(np.interp((p - p0) / (p1 - p0), Td, pj))
                return old(q)

            distance_at = retimed
            md, mx, my, ax, ay, cx, cy, sy = journey_pass(distance_at)

    outro_b = np.zeros(n)
    intro_b = np.zeros(n)

    # rule-of-thirds lead room: along the direction of travel the camera is placed
    # ahead of the marker so the marker sits near the trailing third line with open
    # space in front; the cross-track offset from the framing layer is kept.
    lead = mode["lead"] if cfg.composition == "thirds" else 0.0
    if lead > 0 and n > 3:
        win = max(1, int(0.25 * fps))
        dx = np.zeros(n)
        dy = np.zeros(n)
        for f in range(n):
            if phase[f] != 1:
                continue
            a0, b0 = max(0, f - win), min(n - 1, f + win)
            sxf = sy[f] * aspect
            vx = (ax[b0] - ax[a0]) / max(1, b0 - a0) * fps / sxf
            vy = (ay[b0] - ay[a0]) / max(1, b0 - a0) * fps / sy[f]
            speed = math.hypot(vx, vy)
            if speed < 1e-9:
                continue
            ux, uy = vx / speed, vy / speed
            k = smoothstep(speed / 0.12)
            ox = (cx[f] - ax[f]) / sxf
            oy = (cy[f] - ay[f]) / sy[f]
            along = ox * ux + oy * uy
            target = lead / 6.0                      # 1/6 of the frame = distance centre → third line
            corr = (target - along) * k
            dx[f] = ux * corr * sxf
            dy[f] = uy * corr * sy[f]
        sig = 0.6 * fps
        cx += np.asarray(gaussian_smooth(dx.tolist(), sig))
        cy += np.asarray(gaussian_smooth(dy.tolist(), sig))

    # manual keyframes (Director's Cut) — they take precedence over automatic framing
    times = np.arange(n) / fps
    kf_weight = np.zeros(n)
    for kf in sorted(cfg.keyframes, key=lambda k: k.time_s):
        ease = EASINGS.get(kf.ease, smootherstep)
        kx, ky = latlon_to_meters(kf.lat, kf.lon)
        kspan = max(0.2, kf.span_km) * 1000.0 / max(0.05, math.cos(math.radians(kf.lat)))
        ramp = max(1.0 / fps, kf.ramp_s)
        t0, t1 = kf.time_s - ramp, kf.time_s
        t2, t3 = kf.time_s + kf.hold_s, kf.time_s + kf.hold_s + ramp
        for f in range(n):
            t = times[f]
            if t <= t0 or t >= t3:
                continue
            w = ease((t - t0) / ramp) if t < t1 else 1.0 if t <= t2 else ease((t3 - t) / ramp)
            kf_weight[f] = max(kf_weight[f], w)
            ux = kx + WORLD_SPAN * round((cx[f] - kx) / WORLD_SPAN)
            cx[f], cy[f], sy[f] = zoom_pan_interpolator((cx[f], cy[f], sy[f]), (ux, ky, kspan))(w)

    # intro / outro blends with the overview viewport
    ov = overview_viewport(journey.xs, journey.ys, aspect)
    if cfg.overview_bottom_reserve > 0:
        r = min(0.4, cfg.overview_bottom_reserve)
        sy_r = ov[2] / (1.0 - r)
        ov = (ov[0], ov[1] - sy_r * r / 2.0, sy_r)
    for f in range(n):
        if phase[f] == 0 and intro_f > 0:
            w = 1.0 - smootherstep(f / max(1, intro_f - 1)) if intro_f > 1 else 0.0
            intro_b[f] = w
        elif phase[f] == 2:
            trans_f = max(1, min(outro_f, int(round(outro_tr_s * fps))))
            w = smootherstep((f - intro_f - journey_f + 1) / trans_f)
            outro_b[f] = w
        else:
            continue
        w = intro_b[f] if phase[f] == 0 else outro_b[f]
        cx[f], cy[f], sy[f] = zoom_pan_interpolator((cx[f], cy[f], sy[f]), ov)(w)

    # final per-frame low-pass (≈2 frames)
    cx = np.asarray(gaussian_smooth(cx.tolist(), 1.5))
    cy = np.asarray(gaussian_smooth(cy.tolist(), 1.5))
    sy = np.exp(np.asarray(gaussian_smooth(np.log(sy).tolist(), 1.5)))

    # marker visibility guard, soft: the corrections a hard clamp would need (keep the marker
    # inside 40 % of the half-frame) are themselves low-passed in viewport units before they are
    # applied, so the camera eases toward the marker instead of snapping (a hard clamp is only C0
    # and shows up as a visible kink). A final hard clamp at 46 % remains as a safety net.
    def _guard_corr(limit: float) -> Tuple[np.ndarray, np.ndarray]:
        ox = np.zeros(n)
        oy = np.zeros(n)
        for f in range(n):
            if phase[f] != 1 or kf_weight[f] > 0:
                continue
            hx, hy = sy[f] * aspect * limit, sy[f] * limit
            if mx[f] < cx[f] - hx:
                ox[f] = (mx[f] + hx - cx[f]) / (sy[f] * aspect)
            elif mx[f] > cx[f] + hx:
                ox[f] = (mx[f] - hx - cx[f]) / (sy[f] * aspect)
            if my[f] < cy[f] - hy:
                oy[f] = (my[f] + hy - cy[f]) / sy[f]
            elif my[f] > cy[f] + hy:
                oy[f] = (my[f] - hy - cy[f]) / sy[f]
        return ox, oy

    ox, oy = _guard_corr(0.40)
    if np.any(ox) or np.any(oy):
        sig_g = max(1.0, 0.22 * fps)
        # widen each correction bump before smoothing so its peak survives the low-pass
        win = int(sig_g)

        def _spread(a):
            if win < 1:
                return a
            pad = np.pad(a, win, mode="edge")
            out = a.copy()
            for k in range(len(a)):
                seg = pad[k:k + 2 * win + 1]
                out[k] = seg[np.argmax(np.abs(seg))]
            return out
        ox = np.asarray(gaussian_smooth(_spread(ox).tolist(), sig_g))
        oy = np.asarray(gaussian_smooth(_spread(oy).tolist(), sig_g))
        cx = cx + ox * sy * aspect
        cy = cy + oy * sy
    hx_, hy_ = _guard_corr(0.46)
    cx = cx + hx_ * sy * aspect
    cy = cy + hy_ * sy
    sy = np.minimum(sy, MAX_SPAN * 1.2)
    return FramePlan(fps, width, height, cx, cy, sy, mx, my, md, phase, outro_b, intro_b, ov)


def jerk_report(plan: FramePlan) -> Dict[str, float]:
    """Section VI.1 verification: spikes in the camera's second derivative.

    Positions are expressed in viewport units (divided by span) so zoomed-in
    and zoomed-out motion are comparable. Returns the worst spike ratio (max
    |Δ²| over median |Δ²|) for x, y and log-scale, and the max absolute Δ² in
    viewport units per frame².
    """
    s = plan.span_y
    # evaluate Δ² in a frame-local way: use span of the frame to normalise movement
    cxn = np.diff(plan.cx) / (s[1:] * plan.aspect)
    cyn = np.diff(plan.cy) / s[1:]
    acc_x = np.abs(np.diff(cxn)) if len(cxn) > 1 else np.zeros(1)
    acc_y = np.abs(np.diff(cyn)) if len(cyn) > 1 else np.zeros(1)
    acc_z = np.abs(np.diff(np.diff(np.log(s)))) if len(s) > 2 else np.zeros(1)
    return {
        "max_accel_x_vp": float(acc_x.max()),
        "max_accel_y_vp": float(acc_y.max()),
        "max_accel_zoom": float(acc_z.max()),
        "spike_ratio_zoom": float(spike_ratio(np.log(s).tolist())),
        "frames": plan.frame_count,
    }


def upstream_style_track_jerk(journey: Journey, mode: str, width: int, height: int, fps: int,
                              duration_s: float) -> Dict[str, float]:
    """Jerk metrics of the uncorrected upstream path (linear interpolation, no low-pass)
    for comparison in tests/benchmarks."""
    aspect = width / height
    geo = RouteGeometry(journey)
    mode = LEGACY_CAMERA_MODES.get(mode, mode)
    dist = visual_timing(geo, mode, aspect, "balanced", "balanced", True)
    track = build_track(geo, mode, dist, 480, aspect, "balanced", "balanced")
    n = int(duration_s * fps)
    cx = np.zeros(n)
    cy = np.zeros(n)
    sy = np.zeros(n)
    for f in range(n):
        pos = f / max(1, n - 1) * 480
        i = min(int(pos), 479)
        t = pos - i
        cx[f] = lerp(track[i, 0], track[i + 1, 0], t)
        cy[f] = lerp(track[i, 1], track[i + 1, 1], t)
        sy[f] = math.exp(lerp(math.log(track[i, 2]), math.log(track[i + 1, 2]), t))
    fp = FramePlan(fps, width, height, cx, cy, sy, cx, cy, cx, np.ones(n, np.uint8), np.zeros(n),
                   np.zeros(n), (0, 0, 1))
    return jerk_report(fp)
