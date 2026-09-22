"""Timeline data model.

Points are stored column-wise in numpy arrays so multi-hundred-MB exports stay
within a sane memory budget. Instants are UTC epoch seconds; ``offset_min``
keeps the source UTC offset for local-calendar date filtering. When the
source omitted a timezone the wall time is stored as if UTC and
``tz_missing`` is True — such points are never treated as proof of absolute
ordering across records (behaviour inherited from the upstream parser).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import IntEnum
from typing import Dict, List, Optional

import numpy as np


class PointKind(IntEnum):
    VISIT = 0
    ACTIVITY = 1
    PATH = 2
    RAW_SIGNAL = 3
    LEGACY_RECORD = 4
    LEGACY_VISIT = 5
    LEGACY_ACTIVITY = 6


SEMANTIC_KINDS = (PointKind.VISIT, PointKind.ACTIVITY, PointKind.PATH,
                  PointKind.LEGACY_RECORD, PointKind.LEGACY_VISIT, PointKind.LEGACY_ACTIVITY)


@dataclass
class PointColumns:
    t: np.ndarray            # float64 epoch seconds (UTC, or wall-time-as-UTC if tz_missing)
    offset_min: np.ndarray   # int16 source UTC offset in minutes (0 if missing)
    tz_missing: np.ndarray   # bool
    lat: np.ndarray          # float64
    lon: np.ndarray          # float64
    kind: np.ndarray         # uint8 PointKind
    accuracy: np.ndarray     # float32 metres, NaN if unknown
    mode: Optional[np.ndarray] = None   # uint8 transport Mode of the hop *arriving* at this point

    FIELDS = ("t", "offset_min", "tz_missing", "lat", "lon", "kind", "accuracy", "mode")

    def __post_init__(self):
        if self.mode is None:
            self.mode = np.zeros(len(self.t), np.uint8)

    @classmethod
    def empty(cls) -> "PointColumns":
        return cls(np.zeros(0), np.zeros(0, np.int16), np.zeros(0, bool), np.zeros(0),
                   np.zeros(0), np.zeros(0, np.uint8), np.zeros(0, np.float32), np.zeros(0, np.uint8))

    @classmethod
    def from_rows(cls, rows: List[tuple], modes: Optional[List[int]] = None) -> "PointColumns":
        """rows: (t, offset_min, tz_missing, lat, lon, kind, accuracy)"""
        if not rows:
            return cls.empty()
        arr = list(zip(*rows))
        return cls(np.asarray(arr[0], np.float64), np.asarray(arr[1], np.int16),
                   np.asarray(arr[2], bool), np.asarray(arr[3], np.float64),
                   np.asarray(arr[4], np.float64), np.asarray(arr[5], np.uint8),
                   np.asarray([np.nan if a is None else a for a in arr[6]], np.float32),
                   np.asarray(modes, np.uint8) if modes is not None else None)

    def __len__(self) -> int:
        return int(self.t.shape[0])

    def take(self, idx) -> "PointColumns":
        return PointColumns(self.t[idx], self.offset_min[idx], self.tz_missing[idx],
                            self.lat[idx], self.lon[idx], self.kind[idx], self.accuracy[idx],
                            self.mode[idx])

    @classmethod
    def concat(cls, parts: List["PointColumns"]) -> "PointColumns":
        return cls(*(np.concatenate([getattr(p, f) for p in parts]) for f in cls.FIELDS))

    def local_dates(self) -> np.ndarray:
        """Local calendar date (as numpy datetime64[D]) of each point."""
        local = self.t + self.offset_min.astype(np.float64) * 60.0
        return (local // 86400).astype("int64").astype("datetime64[D]")

    def to_npz_dict(self, prefix: str) -> Dict[str, np.ndarray]:
        return {f"{prefix}_{k}": getattr(self, k) for k in self.FIELDS}

    @classmethod
    def from_npz(cls, data, prefix: str) -> "PointColumns":
        cols = [data[f"{prefix}_{k}"] for k in cls.FIELDS[:-1]]
        mode = data[f"{prefix}_mode"] if f"{prefix}_mode" in data else None   # caches from 1.x lack it
        return cls(*cols, mode)


@dataclass
class ImportDiagnostics:
    detected_format: str = "unknown"
    containers: List[str] = field(default_factory=list)
    segments_seen: int = 0
    records_seen: int = 0
    skipped: Dict[str, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    direction_reversed: bool = False
    standalone_paths_dropped: int = 0
    duplicates_removed: int = 0
    source_bytes: int = 0
    parse_seconds: float = 0.0
    resumed_from_checkpoint: bool = False

    def skip(self, reason: str, n: int = 1) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + n

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Timeline:
    semantic: PointColumns
    raw: PointColumns
    diagnostics: ImportDiagnostics
    source_path: Optional[str] = None
    source_sha256: Optional[str] = None

    @property
    def point_count(self) -> int:
        return len(self.semantic) + len(self.raw)

    def date_range(self) -> Optional[tuple]:
        cols = self.semantic if len(self.semantic) else self.raw
        if not len(cols):
            return None
        d = cols.local_dates()
        return (d.min().astype(object), d.max().astype(object))

    def points_per_day(self) -> Dict[date, int]:
        cols = self.semantic if len(self.semantic) else self.raw
        if not len(cols):
            return {}
        days, counts = np.unique(cols.local_dates(), return_counts=True)
        return {d.astype(object): int(c) for d, c in zip(days, counts)}


def epoch_to_datetime(t: float, offset_min: int = 0) -> datetime:
    tz = timezone(timedelta(minutes=int(offset_min)))
    return datetime.fromtimestamp(float(t), tz=timezone.utc).astimezone(tz)
