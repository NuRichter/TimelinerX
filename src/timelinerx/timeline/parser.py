"""Timeline import: format detection, extraction, reconciliation, checkpointing.

Semantic-segment extraction (visit / activity / timelinePath, offset-based path
timestamps, descending-export detection, standalone-path reconciliation against
semantic intervals, timezone-missing handling) is adapted from Google Timeline
Visualizer (c) 2025 mahlernim, MIT License. The streaming reader, legacy
Takeout support (``locations``, ``timelineObjects``), ZIP intake, limits and
checkpoint/resume are NuRichter additions.
"""

from __future__ import annotations

import bisect
import hashlib
import io
import json
import os
import time
import zipfile
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from ..core.errors import (ImportFailedError, InputTooLargeError, NoDataError,
                           UnsupportedFormatError)
from ..utils.cancel import NEVER_CANCELLED, CancelToken
from .model import ImportDiagnostics, PointColumns, PointKind, Timeline
from .modes import Mode, mode_from_type
from .streaming import ElementScanner, ScanPosition, is_malformed
from .values import parse_coordinate_detailed, parse_e7_pair, parse_instant

SEGMENT_DIRECTION_SIGNAL_S = timedelta(hours=36).total_seconds()
STREAMING_THRESHOLD_BYTES = 50 * 1024 * 1024
CHECKPOINT_EVERY_BYTES = 16 * 1024 * 1024
KNOWN_CONTAINERS = ("semanticSegments", "rawSignals", "locations", "timelineObjects", "$root")


@dataclass
class ImportLimits:
    max_file_bytes: int = 4 * 1024 ** 3
    max_zip_entries: int = 20_000
    max_zip_ratio: float = 250.0
    max_element_bytes: int = 256 * 1024 * 1024


CHECKPOINT_FORMAT = 2        # 2: activity intervals (transport modes) are checkpointed too

# Row: (t, offset_min, tz_missing, lat, lon, kind, accuracy, o0, o1, o2)
Row = tuple


@dataclass
class _ExtractState:
    canonical: List[Row] = field(default_factory=list)
    standalone: List[Row] = field(default_factory=list)
    raw: List[Row] = field(default_factory=list)
    intervals: List[Tuple[float, float]] = field(default_factory=list)
    anchors: List[Tuple[int, float]] = field(default_factory=list)
    activities: List[Tuple[float, float, int]] = field(default_factory=list)   # (t0, t1, Mode)
    segment_index: int = 0
    raw_index: int = 0
    legacy_index: int = 0


class TimelineExtractor:
    """Consumes container elements one at a time; order-independent until finalize()."""

    def __init__(self, diagnostics: ImportDiagnostics):
        self.d = diagnostics
        self.s = _ExtractState()

    # ------------------------------------------------------------------ dispatch
    def consume(self, container: str, element: Any) -> None:
        if container not in self.d.containers:
            self.d.containers.append(container)
        if is_malformed(element):
            self.d.skip("malformed_record")
            return
        if container in ("semanticSegments", "$root"):
            self._segment(element)
        elif container == "rawSignals":
            self._raw_signal(element)
        elif container == "locations":
            self._legacy_record(element)
        elif container == "timelineObjects":
            self._legacy_object(element)

    # ------------------------------------------------------------ semantic segs
    def _segment(self, seg: Any) -> None:
        idx = self.s.segment_index
        self.s.segment_index += 1
        self.d.segments_seen += 1
        if not isinstance(seg, dict):
            self.d.skip("segment_not_object")
            return
        start_raw, end_raw = seg.get("startTime"), seg.get("endTime")
        start_i = parse_instant(start_raw)
        if start_i is not None:
            self.s.anchors.append((idx, start_i[0]))
        activity, visit = seg.get("activity"), seg.get("visit")
        cand = visit.get("topCandidate") if isinstance(visit, dict) else None
        a_start = parse_coordinate_detailed(activity.get("start"))[0] if isinstance(activity, dict) else None
        a_end = parse_coordinate_detailed(activity.get("end"))[0] if isinstance(activity, dict) else None
        v_loc = parse_coordinate_detailed(cand.get("placeLocation"))[0] if isinstance(cand, dict) else None
        has_semantic = any(c is not None for c in (a_start, a_end, v_loc))
        out = self.s.canonical if has_semantic else self.s.standalone

        path = seg.get("timelinePath", [])
        if isinstance(path, list):
            end_i = parse_instant(end_raw)
            for j, pp in enumerate(path):
                if not isinstance(pp, dict):
                    self.d.skip("path_point_not_object")
                    continue
                ts = self._path_ts(pp, start_i, end_i)
                coord, reason = parse_coordinate_detailed(pp.get("point"))
                if ts is None:
                    self.d.skip("missing_timestamp")
                    continue
                if coord is None:
                    self.d.skip(reason or "invalid_coordinate")
                    continue
                out.append((ts[0], ts[1], ts[2], coord[0], coord[1], int(PointKind.PATH), None,
                            idx, 1, j))

        if has_semantic:
            end_i = parse_instant(end_raw)
            if isinstance(activity, dict):
                self._activity(start_i, end_i, (activity.get("topCandidate") or {}).get("type")
                               if isinstance(activity.get("topCandidate"), dict) else None)
                if start_i is not None and a_start is not None:
                    self._add(self.s.canonical, start_i, a_start, PointKind.ACTIVITY, (idx, 0, 0))
                if end_i is not None and a_end is not None:
                    self._add(self.s.canonical, end_i, a_end, PointKind.ACTIVITY, (idx, 2, 0))
                if start_i is None:
                    self.d.skip("missing_timestamp")
            if isinstance(cand, dict) and v_loc is not None:
                if start_i is not None:
                    self._add(self.s.canonical, start_i, v_loc, PointKind.VISIT, (idx, 0, 1))
                else:
                    self.d.skip("missing_timestamp")
            if (start_i is not None and end_i is not None and not start_i[2] and not end_i[2]
                    and end_i[0] >= start_i[0]):
                self.s.intervals.append((start_i[0], end_i[0]))
        elif isinstance(activity, dict) or isinstance(cand, dict):
            self.d.skip("invalid_coordinate")

    def _activity(self, start_i, end_i, activity_type) -> None:
        """Remember the transport mode of a movement interval (absolute times only)."""
        m = mode_from_type(activity_type)
        if m == Mode.UNKNOWN or start_i is None or end_i is None or start_i[2] or end_i[2]:
            return
        if end_i[0] >= start_i[0]:
            self.s.activities.append((start_i[0], end_i[0], int(m)))

    def _hop_modes(self, rows: List[Row]) -> List[int]:
        """Mode of the hop arriving at each row: the activity interval containing the hop's
        midpoint (rows without absolute time, and the first row, get UNKNOWN)."""
        acts = sorted(self.s.activities)
        if not acts or not rows:
            return [0] * len(rows)
        starts = [a[0] for a in acts]
        out = [0]
        for prev, cur in zip(rows, rows[1:]):
            if prev[2] or cur[2]:
                out.append(0)
                continue
            mid = 0.5 * (prev[0] + cur[0])
            k = bisect.bisect_right(starts, mid) - 1
            m = 0
            # intervals rarely overlap; look back a few for one that still covers ``mid``
            for j in range(k, max(-1, k - 4), -1):
                if acts[j][0] <= mid <= acts[j][1]:
                    m = acts[j][2]
                    break
            out.append(m)
        return out

    @staticmethod
    def _path_ts(pp: dict, start_i, end_i):
        absolute = parse_instant(pp.get("time"))
        if absolute is not None:
            return absolute
        off = pp.get("durationMinutesOffsetFromStartTime")
        if isinstance(off, bool) or start_i is None:
            return None
        try:
            off = int(off)
        except (TypeError, ValueError, OverflowError):
            return None
        if off < 0:
            return None
        t = start_i[0] + off * 60.0
        if end_i is not None and not start_i[2] and not end_i[2] and t > end_i[0] + 60:
            return None
        return (t, start_i[1], start_i[2])

    @staticmethod
    def _add(out: list, inst, coord, kind: PointKind, order, accuracy=None):
        out.append((inst[0], inst[1], inst[2], coord[0], coord[1], int(kind), accuracy) + tuple(order))

    # --------------------------------------------------------------- raw signal
    def _raw_signal(self, sig: Any) -> None:
        i = self.s.raw_index
        self.s.raw_index += 1
        self.d.records_seen += 1
        pos = sig.get("position") if isinstance(sig, dict) else None
        if not isinstance(pos, dict):
            self.d.skip("raw_signal_without_position")
            return
        coord, reason = parse_coordinate_detailed(pos.get("LatLng") or pos.get("latLng"))
        inst = parse_instant(pos.get("timestamp"))
        try:
            acc = float(pos.get("accuracyMeters"))
            if not np.isfinite(acc) or acc < 0:
                acc = None
        except (TypeError, ValueError):
            acc = None
        if coord is None:
            self.d.skip(reason or "invalid_coordinate")
            return
        if inst is None:
            self.d.skip("missing_timestamp")
            return
        self._add(self.s.raw, inst, coord, PointKind.RAW_SIGNAL, (i, 0, 0), acc)

    # ------------------------------------------------------------------- legacy
    def _legacy_record(self, rec: Any) -> None:
        i = self.s.legacy_index
        self.s.legacy_index += 1
        self.d.records_seen += 1
        if not isinstance(rec, dict):
            self.d.skip("record_not_object")
            return
        coord, reason = parse_e7_pair(rec)
        inst = parse_instant(rec.get("timestamp")) or parse_instant(rec.get("timestampMs"))
        if coord is None:
            self.d.skip(reason or "invalid_coordinate")
            return
        if inst is None:
            self.d.skip("missing_timestamp")
            return
        acc = rec.get("accuracy")
        acc = float(acc) if isinstance(acc, (int, float)) and not isinstance(acc, bool) else None
        self._add(self.s.canonical, inst, coord, PointKind.LEGACY_RECORD, (1_000_000_000 + i, 0, 0), acc)

    def _legacy_object(self, obj: Any) -> None:
        i = self.s.legacy_index
        self.s.legacy_index += 1
        self.d.segments_seen += 1
        if not isinstance(obj, dict):
            self.d.skip("segment_not_object")
            return
        order0 = 1_000_000_000 + i

        def dur(o):
            d = o.get("duration") if isinstance(o, dict) else None
            if not isinstance(d, dict):
                return None, None
            s = parse_instant(d.get("startTimestamp")) or parse_instant(d.get("startTimestampMs"))
            e = parse_instant(d.get("endTimestamp")) or parse_instant(d.get("endTimestampMs"))
            return s, e

        pv = obj.get("placeVisit")
        if isinstance(pv, dict):
            s, e = dur(pv)
            coord, reason = parse_e7_pair(pv.get("location") or {})
            if coord is None or s is None:
                self.d.skip(reason or "missing_timestamp")
                return
            self._add(self.s.canonical, s, coord, PointKind.LEGACY_VISIT, (order0, 0, 0))
            if s is not None:
                self.s.anchors.append((order0, s[0]))
            return
        seg = obj.get("activitySegment")
        if isinstance(seg, dict):
            s, e = dur(seg)
            self._activity(s, e, seg.get("activityType"))
            if s is not None:
                self.s.anchors.append((order0, s[0]))
            sc, _ = parse_e7_pair(seg.get("startLocation") or {})
            ec, _ = parse_e7_pair(seg.get("endLocation") or {})
            added = False
            if s is not None and sc is not None:
                self._add(self.s.canonical, s, sc, PointKind.LEGACY_ACTIVITY, (order0, 0, 0))
                added = True
            raw_path = (seg.get("simplifiedRawPath") or {}).get("points") or []
            for j, p in enumerate(raw_path if isinstance(raw_path, list) else []):
                if not isinstance(p, dict):
                    continue
                c, _ = parse_e7_pair(p, "latE7", "lngE7")
                t = parse_instant(p.get("timestamp")) or parse_instant(p.get("timestampMs"))
                if c is not None and t is not None:
                    self._add(self.s.canonical, t, c, PointKind.LEGACY_ACTIVITY, (order0, 1, j))
                    added = True
            if e is not None and ec is not None:
                self._add(self.s.canonical, e, ec, PointKind.LEGACY_ACTIVITY, (order0, 2, 0))
                added = True
            if not added:
                self.d.skip("invalid_coordinate")
            return
        self.d.skip("unknown_timeline_object")

    # ----------------------------------------------------------------- finalize
    def finalize(self) -> Tuple[PointColumns, PointColumns]:
        s = self.s
        reversed_, robust = self._descending(s.anchors)
        # ordering follows the upstream decision; the user-facing diagnostic is only raised when
        # the first-to-last span also runs backwards (a lone backwards jump is usually a duplicate)
        self.d.direction_reversed = reversed_ and robust

        def order_key(r: Row):
            o0 = r[7]
            if reversed_ and o0 < 1_000_000_000:
                o0 = -o0
            return (o0, r[8], r[9])

        # merge semantic intervals
        intervals = sorted(s.intervals)
        merged: List[List[float]] = []
        for a, b in intervals:
            if not merged or a > merged[-1][1]:
                merged.append([a, b])
            elif b > merged[-1][1]:
                merged[-1][1] = b

        standalone = self._ordered(s.standalone, order_key)
        kept = list(s.canonical)
        k = 0
        dropped = 0
        for r in standalone:
            t = r[0]
            if r[2]:
                kept.append(r)
                continue
            while k < len(merged) and merged[k][1] < t:
                k += 1
            if k < len(merged) and merged[k][0] <= t <= merged[k][1]:
                dropped += 1
                continue
            kept.append(r)
        self.d.standalone_paths_dropped = dropped

        seen = set()
        unique = []
        for r in self._ordered(kept, order_key):
            key = (r[0], r[3], r[4])
            if key in seen:
                self.d.duplicates_removed += 1
                continue
            seen.add(key)
            unique.append(r)

        raw_sorted = self._ordered(s.raw, order_key)
        return (PointColumns.from_rows([r[:7] for r in unique], self._hop_modes(unique)),
                PointColumns.from_rows([r[:7] for r in raw_sorted]))

    @staticmethod
    def _ordered(rows: List[Row], order_key) -> List[Row]:
        if any(r[2] for r in rows):
            return sorted(rows, key=order_key)
        return sorted(rows, key=lambda r: (r[0],) + order_key(r))

    @staticmethod
    def _descending(anchors: List[Tuple[int, float]]) -> Tuple[bool, bool]:
        a = [t for _, t in sorted(anchors) if _ < 1_000_000_000]
        asc = desc = 0
        endpoint_desc = False
        for prev, cur in zip(a, a[1:]):
            delta = cur - prev
            if abs(delta) < SEGMENT_DIRECTION_SIGNAL_S:
                continue
            if delta > 0:
                asc += 1
            else:
                desc += 1
        if len(a) >= 2:
            ed = a[-1] - a[0]
            if abs(ed) >= SEGMENT_DIRECTION_SIGNAL_S:
                if ed > 0:
                    asc += 2
                else:
                    desc += 2
                    endpoint_desc = True
        return desc > asc, endpoint_desc

    # ---------------------------------------------------------- checkpointing
    def save_state(self, path: Path, position: ScanPosition, fingerprint: str) -> None:
        s = self.s

        def arr(rows):
            return np.asarray([tuple(np.nan if v is None else v for v in r) for r in rows],
                              np.float64).reshape(-1, 10)

        meta = {
            "fingerprint": fingerprint, "format": CHECKPOINT_FORMAT, "position": position.to_dict(),
            "segment_index": s.segment_index, "raw_index": s.raw_index,
            "legacy_index": s.legacy_index, "diagnostics": self.d.to_dict(),
        }
        tmp = path.with_suffix(".tmp.npz")
        np.savez(tmp, canonical=arr(s.canonical), standalone=arr(s.standalone), raw=arr(s.raw),
                 intervals=np.asarray(s.intervals, np.float64).reshape(-1, 2),
                 anchors=np.asarray(s.anchors, np.float64).reshape(-1, 2),
                 activities=np.asarray(s.activities, np.float64).reshape(-1, 3),
                 meta=np.frombuffer(json.dumps(meta).encode(), np.uint8))
        os.replace(tmp, path)

    def load_state(self, path: Path, fingerprint: str) -> Optional[ScanPosition]:
        try:
            with np.load(path, allow_pickle=False) as data:
                meta = json.loads(bytes(data["meta"]).decode())
                if meta.get("fingerprint") != fingerprint or meta.get("format") != CHECKPOINT_FORMAT:
                    return None

                def rows(a):
                    out = []
                    for r in a:
                        acc = None if np.isnan(r[6]) else float(r[6])
                        out.append((float(r[0]), int(r[1]), bool(r[2]), float(r[3]), float(r[4]),
                                    int(r[5]), acc, int(r[7]), int(r[8]), int(r[9])))
                    return out

                s = self.s
                s.canonical = rows(data["canonical"])
                s.standalone = rows(data["standalone"])
                s.raw = rows(data["raw"])
                s.intervals = [tuple(x) for x in data["intervals"].tolist()]
                s.anchors = [(int(a), float(b)) for a, b in data["anchors"].tolist()]
                if "activities" in data:
                    s.activities = [(float(a), float(b), int(c)) for a, b, c in data["activities"].tolist()]
                s.segment_index = meta["segment_index"]
                s.raw_index = meta["raw_index"]
                s.legacy_index = meta["legacy_index"]
                for k, v in meta["diagnostics"].items():
                    setattr(self.d, k, v)
                p = meta["position"]
                return ScanPosition(p["offset"], p["container"], p["root_kind"])
        except (OSError, KeyError, ValueError):
            return None


# ============================================================== public API
ProgressFn = Callable[[float, str], None]


def file_fingerprint(path: Path) -> str:
    st = path.stat()
    h = hashlib.sha256()
    h.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
    with open(path, "rb") as f:
        h.update(f.read(1024 * 1024))
        if st.st_size > 2 * 1024 * 1024:
            f.seek(-1024 * 1024, os.SEEK_END)
            h.update(f.read())
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def detect_format(containers: List[str]) -> str:
    if "semanticSegments" in containers or "rawSignals" in containers:
        return "device-timeline-object (Android/on-device export)"
    if "$root" in containers:
        return "device-timeline-array (iOS export)"
    if "timelineObjects" in containers:
        return "takeout-semantic-location-history (legacy)"
    if "locations" in containers:
        return "takeout-records (legacy Records.json)"
    return "unknown"


def load_timeline(path: str | os.PathLike, *, limits: ImportLimits | None = None,
                  checkpoint_dir: Optional[Path] = None, progress: Optional[ProgressFn] = None,
                  cancel: CancelToken = NEVER_CANCELLED, compute_sha: bool = True,
                  _stop_after_bytes: Optional[int] = None) -> Timeline:
    """Import a Timeline export (.json or Takeout .zip).

    Files larger than 50 MB use the streaming scanner and, when
    ``checkpoint_dir`` is given, write resumable checkpoints every ~16 MB.
    ``_stop_after_bytes`` is a test hook that simulates an interruption.
    """
    limits = limits or ImportLimits()
    p = Path(path)
    if not p.is_file():
        raise ImportFailedError(f"File not found: {p}")
    size = p.stat().st_size
    if size > limits.max_file_bytes:
        raise InputTooLargeError(f"File is {size / 1e9:.2f} GB, above the configured limit "
                                 f"of {limits.max_file_bytes / 1e9:.2f} GB.")
    diag = ImportDiagnostics(source_bytes=size)
    t0 = time.perf_counter()
    extractor = TimelineExtractor(diag)

    if zipfile.is_zipfile(p):
        _load_zip(p, extractor, limits, progress, cancel)
    else:
        with open(p, "rb") as f:
            head = f.read(4096)
        if head.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise UnsupportedFormatError(
                "The file is UTF-16 encoded, which Timeline exports never are.",
                hint="Run Repair: it decodes UTF-16 deterministically (HIGH confidence).")
        if head.startswith(b"\xef\xbb\xbf"):
            diag.warnings.append("UTF-8 byte-order mark ignored.")
            head = head[3:]
        head = head.lstrip()
        if head[:1] not in (b"{", b"["):
            raise UnsupportedFormatError(
                "This file is not JSON (it does not start with '{' or '[').",
                hint="Export Timeline from Google Maps / phone settings as JSON, or pick a Takeout .zip.")
        if size >= STREAMING_THRESHOLD_BYTES or _stop_after_bytes is not None:
            _load_streaming(p, extractor, limits, checkpoint_dir, progress, cancel, _stop_after_bytes)
        else:
            _load_small(p, extractor, progress)

    semantic, raw = extractor.finalize()
    diag.detected_format = detect_format(diag.containers)
    diag.parse_seconds = time.perf_counter() - t0
    if diag.detected_format == "unknown":
        raise UnsupportedFormatError(
            "This JSON does not contain supported Timeline data "
            "(no semanticSegments, rawSignals, timelineObjects or locations).")
    if len(semantic) == 0 and len(raw) == 0:
        raise NoDataError("The file was read, but no usable points were found.",
                          hint="See the diagnostics for skipped record reasons.")
    return Timeline(semantic=semantic, raw=raw, diagnostics=diag, source_path=str(p),
                    source_sha256=sha256_file(p) if compute_sha else None)


def _load_small(p: Path, ex: TimelineExtractor, progress) -> None:
    try:
        with open(p, "rb") as f:
            data = json.loads(f.read().decode("utf-8-sig"))
    except RecursionError as e:
        raise ImportFailedError("JSON nesting is too deep to be a Timeline export.") from e
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise UnsupportedFormatError(
            f"Could not decode JSON: {e}",
            hint="The file may be truncated or corrupted. The repair flow can attempt recovery.") from e
    _feed_document(data, ex)
    if progress:
        progress(1.0, "parsed")


def _feed_document(data: Any, ex: TimelineExtractor) -> None:
    if isinstance(data, list):
        if "$root" not in ex.d.containers:
            ex.d.containers.append("$root")
        for el in data:
            ex.consume("$root", el)
    elif isinstance(data, dict):
        for key in ("semanticSegments", "rawSignals", "locations", "timelineObjects"):
            val = data.get(key)
            if isinstance(val, list):
                if key not in ex.d.containers:
                    ex.d.containers.append(key)
                for el in val:
                    ex.consume(key, el)
            elif val is not None:
                ex.d.warnings.append(f"'{key}' is present but is not an array; ignored.")
    else:
        raise UnsupportedFormatError("Timeline JSON must start with an object or array.")


def _load_streaming(p: Path, ex: TimelineExtractor, limits: ImportLimits,
                    checkpoint_dir: Optional[Path], progress, cancel: CancelToken,
                    stop_after: Optional[int]) -> None:
    size = p.stat().st_size
    fp = file_fingerprint(p)
    ckpt = None
    resume = None
    if checkpoint_dir is not None:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        ckpt = checkpoint_dir / f"import-{fp[:24]}.npz"
        if ckpt.exists():
            resume = ex.load_state(ckpt, fp)
            if resume is not None:
                ex.d.resumed_from_checkpoint = True
    last_ckpt_offset = resume.offset if resume else 0
    with open(p, "rb") as f:
        scanner = ElementScanner(f, max_element_bytes=limits.max_element_bytes, resume=resume)
        for container, element, pos in scanner.elements():
            if container in KNOWN_CONTAINERS:
                ex.consume(container, element)
            if pos.offset - last_ckpt_offset >= CHECKPOINT_EVERY_BYTES:
                cancel.raise_if_cancelled()
                if ckpt is not None:
                    ex.save_state(ckpt, pos, fp)
                last_ckpt_offset = pos.offset
                if progress:
                    progress(min(0.99, pos.offset / max(1, size)), "parsing")
                if stop_after is not None and pos.offset >= stop_after:
                    raise ImportFailedError("simulated interruption (test hook)")
    if ckpt is not None and ckpt.exists():
        ckpt.unlink()
    if progress:
        progress(1.0, "parsed")


def _load_zip(p: Path, ex: TimelineExtractor, limits: ImportLimits, progress, cancel) -> None:
    try:
        zf = zipfile.ZipFile(p)
    except zipfile.BadZipFile as e:
        raise UnsupportedFormatError(f"Corrupted ZIP: {e}") from e
    with zf:
        infos = zf.infolist()
        if len(infos) > limits.max_zip_entries:
            raise InputTooLargeError(f"ZIP has {len(infos)} entries; limit is {limits.max_zip_entries}.")
        candidates = []
        total = 0
        for info in infos:
            name = info.filename.replace("\\", "/")
            if info.is_dir() or not name.lower().endswith(".json"):
                continue
            if name.startswith("/") or ".." in name.split("/"):
                ex.d.warnings.append(f"Skipped unsafe ZIP entry name: {name!r}")
                continue
            if info.compress_size > 0 and info.file_size / info.compress_size > limits.max_zip_ratio:
                raise InputTooLargeError(
                    f"ZIP entry {name!r} has a compression ratio of "
                    f"{info.file_size / info.compress_size:.0f}:1 (possible decompression bomb).")
            total += info.file_size
            candidates.append((name, info))
        if total > limits.max_file_bytes:
            raise InputTooLargeError("Uncompressed ZIP content exceeds the configured size limit.")
        lower = {n.lower(): (n, i) for n, i in candidates}
        primary = [v for k, v in lower.items() if k.endswith(("timeline.json", "records.json",
                                                              "location-history.json"))]
        chosen = primary or [v for k, v in lower.items() if "semantic location history" in k] or candidates
        if not chosen:
            raise UnsupportedFormatError("The ZIP contains no JSON files.")
        ex.d.warnings.append("ZIP entries imported: " + ", ".join(n for n, _ in chosen[:20])
                             + (" …" if len(chosen) > 20 else ""))
        done = 0
        for name, info in chosen:
            cancel.raise_if_cancelled()
            with zf.open(info) as raw:
                limited = _LimitedReader(raw, info.file_size + 1)
                scanner = ElementScanner(limited, max_element_bytes=limits.max_element_bytes)
                for container, element, _ in scanner.elements():
                    if container in KNOWN_CONTAINERS:
                        ex.consume(container, element)
            done += info.file_size
            if progress:
                progress(done / max(1, total), f"parsed {name}")


class _LimitedReader(io.RawIOBase):
    """Guards against ZIP headers that under-report the real uncompressed size."""

    def __init__(self, raw, limit: int):
        self.raw = raw
        self.remaining = limit

    def read(self, n: int = -1) -> bytes:
        data = self.raw.read(n)
        self.remaining -= len(data)
        if self.remaining < 0:
            raise InputTooLargeError("ZIP entry is larger than its header declares.")
        return data

    def readable(self) -> bool:
        return True

    def seek(self, *_a, **_k):  # pragma: no cover - zip streams are not resumable
        raise OSError("not seekable")
