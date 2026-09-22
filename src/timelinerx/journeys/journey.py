"""Journey model: the route for a selected period, ready for camera planning.

Trip-leg detection (``transfer_threshold_km`` / ``build_legs``), the monotone
Hermite distance-compression pacing and the Journal-style detailed-first
route fusion are adapted from Google Timeline Visualizer (c) 2025 mahlernim,
MIT License.
"""

from __future__ import annotations

import bisect
import math
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from ..core.errors import NoDataError
from ..core.geo import (WORLD_SPAN, cumulative_km, haversine_km, interpolate_great_circle, latlon_to_meters,
                        position_at_distance, project_route)
from ..timeline.model import PointColumns, Timeline, epoch_to_datetime
from ..timeline.modes import ARC_MIN_KM, Mode, infer_flight
from ..timeline.outliers import filter_excursions

COMPRESSION_EXPONENTS = {"natural": 1.00, "balanced": 0.85, "faster": 0.75, "fastest": 0.65,
                         # 0.x names
                         "off": 1.00, "gentle": 0.92, "strong": 0.75, "stronger": 0.65}
TRIP_DETECTION_MULTIPLIERS = {"conservative": 1.35, "balanced": 1.00, "sensitive": 0.70}
MIN_TRANSFER_THRESHOLD_KM = 60.0
MAX_TRANSFER_THRESHOLD_KM = 120.0
TRANSFER_TO_TYPICAL_RATIO = 3.0
DEVIATION_MULTIPLIER = 6.0

Leg = Tuple[float, float, bool]  # (start_km, end_km, is_transfer)

ARC_BULGE = 0.16          # flight arcs: apex offset as a fraction of the chord (in projected metres)
ARC_TABLE_SAMPLES = 96


def _arc_control(x0, y0, x1, y1):
    """Quadratic Bézier control point of a flight arc. Arcs always bow toward the top of the map
    (north), the convention of flight-route maps; an out-and-back pair therefore shares one arc
    instead of forming a lens. Due north/south flights bow east."""
    mx, my = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    dx, dy = x1 - x0, y1 - y0
    L = math.hypot(dx, dy)
    if L <= 0:
        return mx, my
    nx, ny = -dy / L, dx / L
    if ny < -1e-9 or (abs(ny) <= 1e-9 and nx < 0):
        nx, ny = -nx, -ny
    k = 2.0 * ARC_BULGE * L
    return mx + nx * k, my + ny * k


class _Arc:
    """Arc-length parameterised quadratic Bézier: constant on-screen speed along the arc."""

    __slots__ = ("p0", "c", "p1", "u", "s")

    def __init__(self, x0, y0, x1, y1):
        self.p0 = (x0, y0)
        self.p1 = (x1, y1)
        self.c = _arc_control(x0, y0, x1, y1)
        t = np.linspace(0.0, 1.0, ARC_TABLE_SAMPLES + 1)
        xs, ys = self._bez(t)
        seg = np.hypot(np.diff(xs), np.diff(ys))
        cs = np.concatenate([[0.0], np.cumsum(seg)])
        self.u = t
        self.s = cs / cs[-1] if cs[-1] > 0 else t

    def _bez(self, t):
        a = (1 - t) ** 2
        b = 2 * (1 - t) * t
        c = t ** 2
        return (a * self.p0[0] + b * self.c[0] + c * self.p1[0],
                a * self.p0[1] + b * self.c[1] + c * self.p1[1])

    def at(self, frac):
        """Point at arc-length fraction ``frac`` (0…1); scalar or array."""
        t = np.interp(frac, self.s, self.u)
        return self._bez(t)

    def tangent(self, frac) -> Tuple[float, float]:
        t = float(np.interp(frac, self.s, self.u))
        dx = 2 * (1 - t) * (self.c[0] - self.p0[0]) + 2 * t * (self.p1[0] - self.c[0])
        dy = 2 * (1 - t) * (self.c[1] - self.p0[1]) + 2 * t * (self.p1[1] - self.c[1])
        return dx, dy


@dataclass
class JourneyConfig:
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    route_source: str = "semantic"        # semantic | detailed
    outlier_filter: str = "conservative"  # conservative | off
    trip_detection: str = "balanced"
    max_accuracy_m: float = 100.0


@dataclass
class Journey:
    t: np.ndarray
    offset_min: np.ndarray
    lats: List[float]
    lons: List[float]
    xs: List[float]
    ys: List[float]
    cum_km: List[float]
    legs: List[Leg]
    outliers_removed: int = 0
    stats: Dict[str, object] = field(default_factory=dict)
    hop_mode: Optional[np.ndarray] = None      # uint8 Mode of hop (i-1 → i); index 0 unused
    arcs: Dict[int, "_Arc"] = field(default_factory=dict)   # hop index → flight arc

    def __post_init__(self):
        if self.hop_mode is None:
            self.hop_mode = np.zeros(len(self.lats), np.uint8)

    def hop_index(self, d: float) -> int:
        """Index i of the hop (i-1 → i) that contains distance ``d`` (≥ 1)."""
        c = self.cum_km
        if len(c) < 2:
            return 0
        d = max(0.0, min(c[-1], d))
        return min(max(bisect.bisect_left(c, d), 1), len(c) - 1)

    def mode_at(self, d: float) -> int:
        return int(self.hop_mode[self.hop_index(d)]) if len(self.cum_km) > 1 else 0

    def hop_fraction(self, d: float) -> Tuple[int, float]:
        i = self.hop_index(d)
        seg = self.cum_km[i] - self.cum_km[i - 1] if i > 0 else 0.0
        return i, (0.0 if seg <= 0 else (max(0.0, min(self.cum_km[-1], d)) - self.cum_km[i - 1]) / seg)

    def xy_at(self, d: float) -> Tuple[float, float]:
        """Projected (Web Mercator, unwrapped) position at route distance ``d``: great-circle
        interpolation on ground hops, the flight arc on flight hops."""
        if len(self.cum_km) < 2:
            return self.xs[0], self.ys[0]
        i, fr = self.hop_fraction(d)
        arc = self.arcs.get(i)
        if arc is not None:
            x, y = arc.at(fr)
            return float(x), float(y)
        lat, lon = interpolate_great_circle(self.lats[i - 1], self.lons[i - 1], self.lats[i], self.lons[i], fr)
        x, y = latlon_to_meters(lat, lon)
        ref = self.xs[i - 1] if fr < 0.5 else self.xs[i]
        x += WORLD_SPAN * round((ref - x) / WORLD_SPAN)
        return x, y

    def direction_at(self, d: float) -> Tuple[float, float]:
        """Direction of travel (projected metres, unnormalised) at ``d``."""
        i, fr = self.hop_fraction(d)
        arc = self.arcs.get(i)
        if arc is not None:
            return arc.tangent(fr)
        return self.xs[i] - self.xs[i - 1], self.ys[i] - self.ys[i - 1]

    @property
    def total_km(self) -> float:
        return self.cum_km[-1] if self.cum_km else 0.0

    @property
    def trip_count(self) -> int:
        return sum(1 for leg in self.legs if leg[2])

    def index_at_distance(self, d: float) -> int:
        return min(max(bisect.bisect_right(self.cum_km, d) - 1, 0), len(self.cum_km) - 1)

    def position(self, d: float) -> Tuple[float, float]:
        return position_at_distance(self.cum_km, self.lats, self.lons, d)

    def datetime_at_distance(self, d: float):
        i = self.index_at_distance(d)
        return epoch_to_datetime(self.t[i], int(self.offset_min[i]))


def _select_period(cols: PointColumns, cfg: JourneyConfig) -> PointColumns:
    if not len(cols):
        return cols
    mask = np.ones(len(cols), bool)
    days = cols.local_dates()
    if cfg.start_date is not None:
        mask &= days >= np.datetime64(cfg.start_date, "D")
    if cfg.end_date is not None:
        mask &= days <= np.datetime64(cfg.end_date, "D")
    return cols.take(np.nonzero(mask)[0])


def fuse_detailed_route(semantic: PointColumns, raw: PointColumns, max_accuracy_m: float
                        ) -> Tuple[PointColumns, Dict[str, int]]:
    """Journal-style detailed-first fusion (raw signals where dense, semantic elsewhere)."""
    stats = {"detailed_input": len(raw), "detailed_usable": 0, "detailed_islands": 0,
             "semantic_backup": len(semantic)}
    if not len(raw):
        return semantic, stats
    acc = raw.accuracy
    keep = np.isfinite(acc) & (acc <= max_accuracy_m)
    r = raw.take(np.nonzero(keep)[0])
    rows = list(zip(r.t.tolist(), r.lat.tolist(), r.lon.tolist(), r.accuracy.tolist(),
                    range(len(r))))
    # one coordinate per identical timestamp (ambiguous groups are dropped)
    norm = []
    i = 0
    while i < len(rows):
        j = i + 1
        while j < len(rows) and rows[j][0] == rows[i][0]:
            j += 1
        group = {}
        for row in rows[i:j]:
            k = (row[1], row[2])
            if k not in group or row[3] < group[k][3]:
                group[k] = row
        if len(group) == 1:
            norm.append(next(iter(group.values())))
        i = j
    # spike removal
    ws = []
    if len(norm) < 3:
        ws = norm
    else:
        ws.append(norm[0])
        for k in range(1, len(norm) - 1):
            b, c, a = ws[-1], norm[k], norm[k + 1]
            window = a[0] - b[0]
            rejoin = max(0.2, (b[3] + a[3]) * 2.0 / 1000.0)
            ing = haversine_km(b[1], b[2], c[1], c[2])
            egr = haversine_km(c[1], c[2], a[1], a[2])
            min_spike = max(0.5, c[3] * 5.0 / 1000.0)
            ih = (c[0] - b[0]) / 3600.0
            eh = (a[0] - c[0]) / 3600.0
            spike = (0 <= window <= 1200 and haversine_km(b[1], b[2], a[1], a[2]) <= rejoin
                     and ing >= min_spike and egr >= min_spike
                     and (ih <= 0 or ing / ih > 250.0) and (eh <= 0 or egr / eh > 250.0))
            if not spike:
                ws.append(c)
        ws.append(norm[-1])
    # stabilisation (merge overlapping fixes)
    stab = []
    for c in ws:
        if not stab:
            stab.append(c)
            continue
        p = stab[-1]
        el = c[0] - p[0]
        unc = max(0.025, (p[3] + c[3]) / 1000.0)
        if 0 <= el <= 600 and haversine_km(p[1], p[2], c[1], c[2]) <= unc:
            if c[3] < p[3]:
                stab[-1] = c
        else:
            stab.append(c)
    if not stab:
        return semantic, stats
    islands: List[List[tuple]] = []
    for p in stab:
        if not islands or p[0] - islands[-1][-1][0] > 1800:
            islands.append([p])
        else:
            islands[-1].append(p)
    stats.update(detailed_usable=len(stab), detailed_islands=len(islands))
    detailed_idx = [p[4] for p in stab]
    detailed = r.take(np.asarray(detailed_idx, int))
    if bool(detailed.tz_missing.any()) or bool(semantic.tz_missing.any()):
        stats["semantic_backup"] = 0
        return detailed, stats
    cov = [(isl[0][0], isl[-1][0]) for isl in islands]
    keep_sem = []
    k = 0
    for idx, t in enumerate(semantic.t.tolist()):
        while k < len(cov) and cov[k][1] < t:
            k += 1
        if not (k < len(cov) and cov[k][0] <= t <= cov[k][1]):
            keep_sem.append(idx)
    stats["semantic_backup"] = len(keep_sem)
    backup = semantic.take(np.asarray(keep_sem, int))
    allc = PointColumns.concat([detailed, backup])
    order = np.argsort(allc.t, kind="stable")
    return allc.take(order), stats


def transfer_threshold_km(cum: List[float], trip_detection: str = "balanced") -> float:
    mult = TRIP_DETECTION_MULTIPLIERS.get(trip_detection, 1.0)
    hops = [b - a for a, b in zip(cum, cum[1:])]
    ordinary = sorted(h for h in hops if 0 < h < MAX_TRANSFER_THRESHOLD_KM)
    if not ordinary:
        return MAX_TRANSFER_THRESHOLD_KM * mult
    typical = statistics.median(ordinary)
    dev = statistics.median(sorted(abs(h - typical) for h in ordinary))
    th = max(MIN_TRANSFER_THRESHOLD_KM, typical * TRANSFER_TO_TYPICAL_RATIO, typical + dev * DEVIATION_MULTIPLIER)
    return min(MAX_TRANSFER_THRESHOLD_KM, th) * mult


def build_legs(cum: List[float], threshold_km: float, flights: Optional[np.ndarray] = None) -> List[Leg]:
    """Split the route into local episodes and long trips ("transfers").

    A hop is a long trip when it is at least ``threshold_km`` long, or when it is a flight of
    at least ARC_MIN_KM (``flights[i]`` refers to the hop arriving at point i)."""
    if len(cum) < 2 or cum[-1] <= 0:
        return []
    legs: List[Leg] = []
    local_start = 0.0
    for k, (a, b) in enumerate(zip(cum, cum[1:])):
        flight = flights is not None and bool(flights[k + 1]) and b - a >= ARC_MIN_KM
        if b - a < max(1.0, threshold_km) and not flight:
            continue
        if a > local_start:
            legs.append((local_start, a, False))
        legs.append((a, b, True))
        local_start = b
    if cum[-1] > local_start:
        legs.append((local_start, cum[-1], False))
    return legs


def select_route_points(timeline: Timeline, cfg: JourneyConfig) -> Tuple[PointColumns, Dict[str, object]]:
    """Normalize step: period selection and route-source choice."""
    sem = _select_period(timeline.semantic, cfg)
    stats: Dict[str, object] = {}
    if cfg.route_source == "detailed":
        raw = _select_period(timeline.raw, cfg)
        cols, fstats = fuse_detailed_route(sem, raw, cfg.max_accuracy_m)
        stats["fusion"] = fstats
    else:
        cols = sem if len(sem) else _select_period(timeline.raw, cfg)
        if not len(sem) and len(cols):
            stats["note"] = "No semantic points in period; raw signals used."
    if not len(cols):
        raise NoDataError("No points found in the selected period.",
                          hint="Choose a period inside the Timeline's date range.")
    return cols, stats


def filter_route_points(cols: PointColumns, cfg: JourneyConfig) -> Tuple[PointColumns, int]:
    """Filter step: GPS teleport excursions."""
    if cfg.outlier_filter == "off" or len(cols) < 3:
        return cols, 0
    kept, removed = filter_excursions(cols.t.tolist(), cols.lat.tolist(), cols.lon.tolist())
    return cols.take(np.asarray(kept, int)), removed


def journey_from_columns(cols: PointColumns, trip_detection: str = "balanced", removed: int = 0,
                         stats: Optional[Dict[str, object]] = None) -> Journey:
    if not len(cols):
        raise NoDataError("No points remain after filtering.")
    stats = dict(stats or {})
    lats = cols.lat.tolist()
    lons = cols.lon.tolist()
    xs, ys = project_route(lats, lons)
    cum = cumulative_km(lats, lons)
    hop_mode = np.asarray(cols.mode, np.uint8).copy()
    if len(hop_mode):
        hop_mode[0] = 0
    t = np.asarray(cols.t, float)
    inferred = 0
    for i in range(1, len(cum)):
        if hop_mode[i] == Mode.UNKNOWN and infer_flight(cum[i] - cum[i - 1], float(t[i] - t[i - 1])):
            hop_mode[i] = Mode.FLIGHT
            inferred += 1
    flights = hop_mode == Mode.FLIGHT
    arcs = {i: _Arc(xs[i - 1], ys[i - 1], xs[i], ys[i]) for i in range(1, len(cum))
            if flights[i] and cum[i] - cum[i - 1] >= ARC_MIN_KM}
    legs = build_legs(cum, transfer_threshold_km(cum, trip_detection), flights)
    first = epoch_to_datetime(cols.t[0], int(cols.offset_min[0]))
    last = epoch_to_datetime(cols.t[-1], int(cols.offset_min[-1]))
    stats.update(points=len(cols), first=first.isoformat(), last=last.isoformat(),
                 days=(last.date() - first.date()).days + 1, total_km=cum[-1],
                 trips=sum(1 for leg in legs if leg[2]), outliers_removed=removed,
                 flights=len(arcs), flights_inferred=inferred,
                 mode_km={int(m): float(sum(cum[i] - cum[i - 1] for i in range(1, len(cum)) if hop_mode[i] == m))
                          for m in set(hop_mode[1:].tolist())})
    return Journey(t=cols.t, offset_min=cols.offset_min, lats=lats, lons=lons, xs=xs, ys=ys,
                   cum_km=cum, legs=legs, outliers_removed=removed, stats=stats, hop_mode=hop_mode, arcs=arcs)


def build_journey(timeline: Timeline, cfg: JourneyConfig) -> Journey:
    cols, stats = select_route_points(timeline, cfg)
    cols, removed = filter_route_points(cols, cfg)
    return journey_from_columns(cols, cfg.trip_detection, removed, stats)


# ------------------------------------------------------------------ pacing
def _endpoint_slope(w1, w2, d1, d2):
    s = ((2 * w1 + w2) * d1 - w1 * d2) / (w1 + w2)
    return 0.0 if s <= 0 else min(s, 3 * d1)


def _monotone_slopes(x: List[float], y: List[float]) -> List[float]:
    n = len(x) - 1
    delta = [(y[i + 1] - y[i]) / (x[i + 1] - x[i]) for i in range(n)]
    if n == 1:
        return [delta[0], delta[0]]
    sl = [0.0] * len(x)
    sl[0] = _endpoint_slope(x[1] - x[0], x[2] - x[1], delta[0], delta[1])
    for i in range(1, len(x) - 1):
        wb = x[i] - x[i - 1]
        wa = x[i + 1] - x[i]
        a1, a2 = 2 * wa + wb, wa + 2 * wb
        if delta[i - 1] <= 0 or delta[i] <= 0:
            sl[i] = 0.0
        else:
            sl[i] = (a1 + a2) / (a1 / delta[i - 1] + a2 / delta[i])
    sl[-1] = _endpoint_slope(x[-1] - x[-2], x[-2] - x[-3], delta[-1], delta[-2])
    return sl


def distance_compression_timing(cum: List[float], compression: str = "balanced") -> Callable[[float], float]:
    """Legacy pacing: long hops get sub-linear time (segment_km ** exponent)."""
    total = cum[-1] if cum else 0.0
    exp = COMPRESSION_EXPONENTS.get(compression, 0.85)
    linear = lambda p: total * max(0.0, min(1.0, p))  # noqa: E731
    if exp >= 1.0 or len(cum) < 2 or total <= 0:
        return linear
    dist = [0.0]
    eff = [0.0]
    et = 0.0
    for a, b in zip(cum, cum[1:]):
        seg = b - a
        if seg <= 0:
            continue
        et += seg ** exp
        dist.append(b)
        eff.append(et)
    if et <= 0 or len(dist) < 3:
        return linear
    xv = [v / et for v in eff]
    sl = _monotone_slopes(xv, dist)

    def f(p: float) -> float:
        e = max(0.0, min(1.0, p))
        ti = min(max(bisect.bisect_left(xv, e), 1), len(xv) - 1)
        fi = ti - 1
        w = xv[ti] - xv[fi]
        t = 0.0 if w <= 0 else (e - xv[fi]) / w
        t2, t3 = t * t, t * t * t
        return ((2 * t3 - 3 * t2 + 1) * dist[fi] + (t3 - 2 * t2 + t) * w * sl[fi]
                + (-2 * t3 + 3 * t2) * dist[ti] + (t3 - t2) * w * sl[ti])

    return f
