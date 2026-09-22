"""GPS outlier detection.

``filter_excursions`` implements the upstream "conservative" teleport-spike
filter (Google Timeline Visualizer, (c) 2025 mahlernim, MIT): a short run of
points that jumps ≥500 km away at an implied speed >1300 km/h and returns to
within 200 km of where it left inside 12 h is removed.

``speed_mad_scores`` is a NuRichter addition: a robust z-score (Median
Absolute Deviation of log-speed) used by the repair engine to *report*
statistically unusual hops without deleting them automatically.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from ..core.geo import haversine_km

EXCURSION_WINDOW_S = 12 * 3600
EXCURSION_RETURN_KM = 200.0
EXCURSION_MIN_JUMP_KM = 500.0
EXCURSION_MIN_SPEED_KMH = 1300.0
EXCURSION_MAX_RUN = 3


def _speed(t0: float, t1: float, km: float) -> float:
    dt = t1 - t0
    if dt <= 0:
        return float("inf")
    return km / (dt / 3600.0)


def _is_excursion(t, lat, lon, before: int, start: int, end: int, after: int) -> bool:
    window = t[after] - t[before]
    if window < 0 or window > EXCURSION_WINDOW_S:
        return False
    if haversine_km(lat[before], lon[before], lat[after], lon[after]) > EXCURSION_RETURN_KM:
        return False
    ingress = haversine_km(lat[before], lon[before], lat[start], lon[start])
    egress = haversine_km(lat[end], lon[end], lat[after], lon[after])
    if ingress < EXCURSION_MIN_JUMP_KM or egress < EXCURSION_MIN_JUMP_KM:
        return False
    if _speed(t[before], t[start], ingress) <= EXCURSION_MIN_SPEED_KMH:
        return False
    if _speed(t[end], t[after], egress) <= EXCURSION_MIN_SPEED_KMH:
        return False
    for i in range(start, end + 1):
        if haversine_km(lat[start], lon[start], lat[i], lon[i]) > EXCURSION_RETURN_KM:
            return False
        if (haversine_km(lat[before], lon[before], lat[i], lon[i]) < EXCURSION_MIN_JUMP_KM
                or haversine_km(lat[i], lon[i], lat[after], lon[after]) < EXCURSION_MIN_JUMP_KM):
            return False
    return True


def find_excursions(t: Sequence[float], lat: Sequence[float], lon: Sequence[float]) -> List[int]:
    """Indices of points belonging to teleport excursions."""
    n = len(t)
    if n < 3:
        return []
    removed: List[int] = []
    last_kept = 0
    i = 1
    while i < n - 1:
        run_end: Optional[int] = None
        for end in range(min(i + EXCURSION_MAX_RUN - 1, n - 2), i - 1, -1):
            if _is_excursion(t, lat, lon, last_kept, i, end, end + 1):
                run_end = end
                break
        if run_end is None:
            last_kept = i
            i += 1
        else:
            removed.extend(range(i, run_end + 1))
            i = run_end + 1
    return removed


def filter_excursions(t, lat, lon) -> Tuple[List[int], int]:
    """Return (kept_indices, removed_count)."""
    removed = set(find_excursions(t, lat, lon))
    kept = [i for i in range(len(t)) if i not in removed]
    return kept, len(removed)


def speed_mad_scores(t: Sequence[float], lat: Sequence[float], lon: Sequence[float]
                     ) -> List[Tuple[int, float, float]]:
    """(index_of_arrival_point, speed_kmh, robust_z) for each hop with dt>0."""
    hops = []
    for i in range(1, len(t)):
        dt = t[i] - t[i - 1]
        if dt <= 0:
            continue
        km = haversine_km(lat[i - 1], lon[i - 1], lat[i], lon[i])
        v = km / (dt / 3600.0)
        hops.append((i, v, math.log10(v + 1.0)))
    if len(hops) < 8:
        return [(i, v, 0.0) for i, v, _ in hops]
    logs = sorted(h[2] for h in hops)
    med = logs[len(logs) // 2]
    mad = sorted(abs(x - med) for x in logs)[len(logs) // 2]
    scale = 1.4826 * mad if mad > 1e-9 else 1e-9
    return [(i, v, (lv - med) / scale) for i, v, lv in hops]
