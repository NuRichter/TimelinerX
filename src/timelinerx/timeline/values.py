"""Parsing of individual coordinate and timestamp values.

``parse_coordinate`` follows the upstream Google Timeline Visualizer rules
(MIT, mahlernim): Android ``"lat°, lon°"``, iOS ``"geo:lat,lon"``, object
wrappers ``{"latLng": ...}`` / ``{"point": ...}``, and E7 integers encoded in
strings. Legacy Takeout ``latitudeE7``/``longitudeE7`` fields are handled by
``parse_e7_pair``.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

from ..core.geo import MAX_LAT

try:  # optional, only used for unusual timestamp spellings
    import dateutil.parser as _dateutil
except Exception:  # pragma: no cover
    _dateutil = None

CoordResult = Tuple[Optional[Tuple[float, float]], Optional[str]]


def parse_coordinate_detailed(value: Any) -> CoordResult:
    """Return ((lat, lon) | None, failure_reason | None)."""
    if isinstance(value, dict):
        value = value.get("latLng") or value.get("LatLng") or value.get("point")
        if isinstance(value, dict):
            return parse_e7_pair(value)
    if not isinstance(value, str) or not value.strip():
        return None, "missing_coordinate"
    cleaned = value.strip()
    if cleaned.startswith("geo:"):
        cleaned = cleaned[4:]
    cleaned = cleaned.split("?", 1)[0].replace("°", "").replace(" ", "")
    parts = cleaned.split(",")
    if len(parts) < 2:
        return None, "malformed_coordinate"
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return None, "malformed_coordinate"
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None, "malformed_coordinate"
    if abs(lat) > 1_000_000 or abs(lon) > 1_000_000:
        lat /= 10_000_000
        lon /= 10_000_000
    if not (-MAX_LAT <= lat <= MAX_LAT and -180 <= lon <= 180):
        return None, "coordinate_out_of_range"
    return (lat, lon), None


def parse_coordinate(value: Any) -> Optional[Tuple[float, float]]:
    return parse_coordinate_detailed(value)[0]


def parse_e7_pair(obj: dict, lat_key: str = "latitudeE7", lon_key: str = "longitudeE7") -> CoordResult:
    lat_raw = obj.get(lat_key, obj.get("latE7"))
    lon_raw = obj.get(lon_key, obj.get("lngE7"))
    if lat_raw is None or lon_raw is None or isinstance(lat_raw, bool) or isinstance(lon_raw, bool):
        return None, "missing_coordinate"
    try:
        lat = int(lat_raw) / 1e7
        lon = int(lon_raw) / 1e7
    except (TypeError, ValueError):
        return None, "malformed_coordinate"
    # Old Takeout exports contain known int32 overflow values for longitude.
    if lat > 90:
        lat -= 4294967296 / 1e7
    if lon > 180:
        lon -= 4294967296 / 1e7
    if not (-MAX_LAT <= lat <= MAX_LAT and -180 <= lon <= 180):
        return None, "coordinate_out_of_range"
    return (lat, lon), None


# (epoch_seconds, offset_minutes, tz_missing)
Instant = Tuple[float, int, bool]


def parse_instant(value: Any) -> Optional[Instant]:
    """Parse ISO-8601 (or epoch-ms digit strings). Missing tz → wall time as UTC."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _from_epoch_ms(value)
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s.isdigit() and len(s) >= 11:
        return _from_epoch_ms(int(s))
    dt = None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        if _dateutil is not None:
            try:
                dt = _dateutil.isoparse(s)
            except (ValueError, OverflowError, TypeError):
                try:
                    dt = _dateutil.parse(s)
                except (ValueError, OverflowError, TypeError):
                    return None
    if dt is None:
        return None
    off = dt.utcoffset()
    if off is None:
        return dt.replace(tzinfo=timezone.utc).timestamp(), 0, True
    try:
        return dt.timestamp(), int(off.total_seconds() // 60), False
    except (OverflowError, OSError, ValueError):
        return None


def _from_epoch_ms(ms: Any) -> Optional[Instant]:
    try:
        v = float(ms) / 1000.0
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or not (-62135596800 < v < 253402300799):
        return None
    return v, 0, False
