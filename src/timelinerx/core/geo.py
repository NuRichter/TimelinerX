"""Geodesy and Web Mercator projection.

``latlon_to_meters``, ``meters_to_latlon``, ``haversine_km``,
``interpolate_great_circle`` and ``position_at_distance`` are adapted from
Google Timeline Visualizer (c) 2025 mahlernim, MIT License.
"""

from __future__ import annotations

import bisect
import math
from typing import Sequence, Tuple

R_EARTH_M = 6378137.0
MAX_EXTENT = 20037508.342789244
WORLD_SPAN = 2 * MAX_EXTENT
MAX_LAT = 85.05112878
EARTH_MEAN_RADIUS_KM = 6371.0088
KM_TO_MILES = 0.621371192237334


def latlon_to_meters(lat: float, lon: float) -> Tuple[float, float]:
    lat_c = max(-MAX_LAT, min(MAX_LAT, lat))
    x = R_EARTH_M * math.radians(lon)
    y = R_EARTH_M * math.log(math.tan(math.pi / 4 + math.radians(lat_c) / 2))
    return x, y


def meters_to_latlon(x: float, y: float) -> Tuple[float, float]:
    lon = math.degrees(x / R_EARTH_M)
    lat = math.degrees(2 * math.atan(math.exp(y / R_EARTH_M)) - math.pi / 2)
    return lat, lon


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return EARTH_MEAN_RADIUS_KM * 2 * math.asin(min(1.0, math.sqrt(a)))


def interpolate_great_circle(lat1: float, lon1: float, lat2: float, lon2: float,
                             fraction: float) -> Tuple[float, float]:
    """Spherical linear interpolation so long hops sweep instead of teleporting."""
    if fraction <= 0:
        return lat1, lon1
    if fraction >= 1:
        return lat2, lon2
    p1, l1 = math.radians(lat1), math.radians(lon1)
    p2, l2 = math.radians(lat2), math.radians(lon2)
    ax, ay, az = math.cos(p1) * math.cos(l1), math.cos(p1) * math.sin(l1), math.sin(p1)
    bx, by, bz = math.cos(p2) * math.cos(l2), math.cos(p2) * math.sin(l2), math.sin(p2)
    dot = max(-1.0, min(1.0, ax * bx + ay * by + az * bz))
    omega = math.acos(dot)
    if math.sin(omega) < 1e-8:
        left, right = 1 - fraction, fraction
    else:
        left = math.sin((1 - fraction) * omega) / math.sin(omega)
        right = math.sin(fraction * omega) / math.sin(omega)
    x, y, z = left * ax + right * bx, left * ay + right * by, left * az + right * bz
    return math.degrees(math.atan2(z, math.sqrt(x * x + y * y))), math.degrees(math.atan2(y, x))


def position_at_distance(cum_km: Sequence[float], lats: Sequence[float], lons: Sequence[float],
                         distance_km: float) -> Tuple[float, float]:
    if not cum_km:
        raise ValueError("A route needs at least one point")
    if len(cum_km) == 1 or cum_km[-1] <= 0:
        return lats[0], lons[0]
    d = max(0.0, min(cum_km[-1], distance_km))
    to_i = min(max(bisect.bisect_left(cum_km, d), 1), len(cum_km) - 1)
    seg = cum_km[to_i] - cum_km[to_i - 1]
    frac = 0.0 if seg <= 0 else (d - cum_km[to_i - 1]) / seg
    return interpolate_great_circle(lats[to_i - 1], lons[to_i - 1], lats[to_i], lons[to_i], frac)


def unwrap_longitudes(lons: Sequence[float]) -> list:
    """Make a longitude sequence continuous across the antimeridian.

    A route Tokyo → Honolulu → LA should not be drawn back across the whole
    map; unwrapped longitudes may exceed ±180 and map onto adjacent world
    copies in projected space.
    """
    out = []
    offset = 0.0
    prev = None
    for lon in lons:
        if prev is not None:
            delta = lon - prev
            if delta > 180:
                offset -= 360
            elif delta < -180:
                offset += 360
        out.append(lon + offset)
        prev = lon
    return out


def project_route(lats: Sequence[float], lons: Sequence[float]):
    """Project lat/lon arrays into continuous Web Mercator meters."""
    ul = unwrap_longitudes(lons)
    xs, ys = [], []
    for lat, lon in zip(lats, ul):
        x, y = latlon_to_meters(lat, lon)
        xs.append(x)
        ys.append(y)
    return xs, ys


def cumulative_km(lats: Sequence[float], lons: Sequence[float]) -> list:
    out = [0.0]
    for i in range(1, len(lats)):
        out.append(out[-1] + haversine_km(lats[i - 1], lons[i - 1], lats[i], lons[i]))
    return out


def format_distance(km: float, unit: str = "km") -> str:
    if unit == "mi":
        return f"{km * KM_TO_MILES:,.1f} mi"
    return f"{km:,.1f} km"
