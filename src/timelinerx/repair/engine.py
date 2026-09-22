"""Forensic repair pipeline (Section X).

Ten ordered passes build a :class:`RepairPlan` — a list of proposed
:class:`RepairAction` objects, each classified HIGH / MEDIUM / LOW confidence
by explicit criteria:

* HIGH   — deterministic, provably correct transforms (BOM/NUL stripping,
           UTF-16 decoding, E7→degrees, exact-duplicate removal). Applied by
           default.
* MEDIUM — statistical or heuristic decisions (teleport excursions, reversed
           export order, recovery of complete records from a truncated file,
           dropping unparseable records). Applied by default, shown clearly,
           individually rejectable.
* LOW    — ambiguous guesses (latitude/longitude swap, endTime<startTime
           swap, isolated statistically unusual hops). Never applied unless
           explicitly accepted.

``apply_plan`` writes ``<stem>.fixed.json``, ``.repair-report.json``,
``.repair-report.html`` and ``.repair-log.txt`` next to (or into an output
directory for) the source. The original file is never modified.
"""

from __future__ import annotations

import codecs
import copy
import html
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import __version__
from ..core.errors import ImportFailedError
from ..timeline.outliers import find_excursions, speed_mad_scores
from ..timeline.parser import ImportLimits, load_timeline
from ..timeline.streaming import ElementScanner, is_malformed
from ..timeline.values import parse_coordinate_detailed, parse_e7_pair, parse_instant

PASSES = [
    "forensic_scan", "structural_recovery", "schema_normalization", "coordinate_repair",
    "timestamp_repair", "ordering_repair", "duplicate_cleanup", "outlier_analysis",
    "semantic_reconstruction", "validation", "repair_report",
]

EARLIEST_PLAUSIBLE = datetime(2005, 1, 1, tzinfo=timezone.utc).timestamp()


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass
class RepairAction:
    id: str
    pass_name: str
    confidence: Confidence
    title: str
    detail: str
    location: str = ""
    before: Any = None
    after: Any = None
    count: int = 1
    applied: bool = False
    # machine-readable operation
    op: Dict[str, Any] = field(default_factory=dict)

    @property
    def default_accept(self) -> bool:
        return self.confidence in (Confidence.HIGH, Confidence.MEDIUM)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["confidence"] = self.confidence.value
        return d


@dataclass
class Finding:
    pass_name: str
    severity: str   # info | warning | error
    message: str


@dataclass
class RepairPlan:
    source: str
    source_bytes: int
    encoding: str = "utf-8"
    document: Any = None                    # recovered (not yet mutated) document
    actions: List[RepairAction] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    pass_timings: Dict[str, float] = field(default_factory=dict)
    structurally_valid: bool = True

    def by_confidence(self, c: Confidence) -> List[RepairAction]:
        return [a for a in self.actions if a.confidence == c]

    def summary(self) -> Dict[str, int]:
        return {c.value: len(self.by_confidence(c)) for c in Confidence}


# =========================================================== pass 1 + 2
def _decode(raw: bytes, plan: RepairPlan) -> str:
    if raw.startswith(codecs.BOM_UTF8):
        plan.actions.append(RepairAction("enc-bom", "structural_recovery", Confidence.HIGH,
                                         "Strip UTF-8 byte-order mark",
                                         "A BOM precedes the JSON text; JSON parsers may reject it."))
        plan.encoding = "utf-8-sig"
        return raw[3:].decode("utf-8", errors="replace")
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        plan.actions.append(RepairAction("enc-utf16", "structural_recovery", Confidence.HIGH,
                                         "Decode UTF-16 text",
                                         "The file is UTF-16 encoded (e.g. re-saved by a Windows editor)."))
        plan.encoding = "utf-16"
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        plan.actions.append(RepairAction("enc-invalid-bytes", "structural_recovery", Confidence.MEDIUM,
                                         "Replace invalid UTF-8 bytes",
                                         f"Invalid UTF-8 at byte {e.start}; bytes replaced with U+FFFD. "
                                         "Affected text is most likely a place name, not a coordinate."))
        return raw.decode("utf-8", errors="replace")


def forensic_scan(path: Path, plan: RepairPlan) -> str:
    raw = path.read_bytes()
    plan.findings.append(Finding("forensic_scan", "info", f"File size {len(raw):,} bytes"))
    text = _decode(raw, plan)
    if "\x00" in text:
        n = text.count("\x00")
        plan.actions.append(RepairAction("enc-nul", "structural_recovery", Confidence.HIGH,
                                         "Remove NUL characters",
                                         f"{n} NUL characters found (typical of interrupted copies).",
                                         count=n))
        text = text.replace("\x00", "")
    stripped = text.strip()
    if not stripped:
        plan.findings.append(Finding("forensic_scan", "error", "The file is empty."))
    elif stripped[0] not in "[{":
        plan.findings.append(Finding("forensic_scan", "error",
                                     "The file does not begin with a JSON object or array."))
    return text


_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def structural_recovery(text: str, plan: RepairPlan) -> Any:
    try:
        return json.loads(text)
    except RecursionError:
        plan.structurally_valid = False
        plan.findings.append(Finding("structural_recovery", "error", "JSON nesting is too deep."))
        return None
    except json.JSONDecodeError as e:
        plan.structurally_valid = False
        plan.findings.append(Finding("structural_recovery", "warning",
                                     f"JSON is invalid at line {e.lineno}, column {e.colno}: {e.msg}"))
    # (a) trailing data after a complete document
    try:
        doc, end = json.JSONDecoder().raw_decode(text.lstrip())
        rest = text.lstrip()[end:].strip()
        if rest:
            plan.actions.append(RepairAction(
                "struct-trailing", "structural_recovery", Confidence.MEDIUM,
                "Discard trailing data after the JSON document",
                f"{len(rest):,} characters follow a complete JSON value (e.g. two files concatenated).",
                before=rest[:120]))
            return doc
    except json.JSONDecodeError:
        pass
    # (b) trailing commas
    fixed = _TRAILING_COMMA.sub(r"\1", text)
    if fixed != text:
        try:
            doc = json.loads(fixed)
            n = len(_TRAILING_COMMA.findall(text))
            plan.actions.append(RepairAction(
                "struct-trailing-commas", "structural_recovery", Confidence.MEDIUM,
                "Remove trailing commas", f"{n} trailing commas before '}}' or ']'.", count=n))
            return doc
        except json.JSONDecodeError:
            pass
    # (c) salvage complete records from a truncated / partly corrupted document
    import io
    scanner = ElementScanner(io.BytesIO(text.encode("utf-8")))
    containers: Dict[str, list] = {}
    malformed = 0
    error = None
    try:
        for container, el, _ in scanner.elements():
            if is_malformed(el):
                malformed += 1
                continue
            containers.setdefault(container, []).append(el)
    except Exception as e:  # noqa: BLE001 — truncated input is expected here
        error = str(e)
    if not containers:
        plan.findings.append(Finding("structural_recovery", "error",
                                     "No complete Timeline records could be recovered."))
        return None
    recovered = sum(len(v) for v in containers.values())
    detail = f"Recovered {recovered:,} complete records from: {', '.join(containers)}."
    if malformed:
        detail += f" {malformed} malformed records were dropped."
    if error:
        detail += f" The document ends abruptly ({error}); records after that point are lost."
    plan.actions.append(RepairAction("struct-salvage", "structural_recovery", Confidence.MEDIUM,
                                     "Rebuild document from complete records", detail,
                                     count=recovered))
    if list(containers) == ["$root"]:
        return containers["$root"]
    return {k: v for k, v in containers.items() if k != "$root"}


# ============================================================ helpers
def _containers(doc: Any) -> List[Tuple[str, list]]:
    if isinstance(doc, list):
        return [("$root", doc)]
    if isinstance(doc, dict):
        return [(k, v) for k, v in doc.items()
                if k in ("semanticSegments", "rawSignals", "locations", "timelineObjects")
                and isinstance(v, list)]
    return []


def _get_container(doc: Any, name: str) -> list:
    return doc if name == "$root" else doc[name]


def _fmt_coord(lat: float, lon: float) -> str:
    return f"{lat:.7f}°, {lon:.7f}°"


# ============================================================ pass 3
def schema_normalization(doc: Any, plan: RepairPlan) -> None:
    n_e7 = 0
    n_ms = 0
    for name, items in _containers(doc):
        if name in ("semanticSegments", "$root"):
            for i, seg in enumerate(items):
                if not isinstance(seg, dict):
                    continue
                for j, pp in enumerate(seg.get("timelinePath") or []):
                    if isinstance(pp, dict) and isinstance(pp.get("point"), str):
                        c, _ = parse_coordinate_detailed(pp["point"])
                        raw = pp["point"].replace("geo:", "").replace("°", "").split(",")
                        try:
                            big = abs(float(raw[0])) > 1_000_000
                        except (ValueError, IndexError):
                            big = False
                        if c is not None and big:
                            n_e7 += 1
                            plan.actions.append(RepairAction(
                                f"schema-e7-{name}-{i}-{j}", "schema_normalization", Confidence.HIGH,
                                "Convert E7 integer coordinate to degrees",
                                "Coordinate stored as E7 integers inside a string.",
                                location=f"{name}[{i}].timelinePath[{j}]", before=pp["point"],
                                after=_fmt_coord(*c),
                                op={"kind": "set", "path": [name, i, "timelinePath", j, "point"],
                                    "value": _fmt_coord(*c)}))
        if name == "locations":
            for i, rec in enumerate(items):
                if isinstance(rec, dict) and "timestamp" not in rec and "timestampMs" in rec:
                    inst = parse_instant(rec["timestampMs"])
                    if inst is not None:
                        n_ms += 1
                        iso = datetime.fromtimestamp(inst[0], timezone.utc).isoformat().replace("+00:00", "Z")
                        plan.actions.append(RepairAction(
                            f"schema-ms-{i}", "schema_normalization", Confidence.HIGH,
                            "Convert epoch-milliseconds timestamp to ISO-8601",
                            "Old Takeout records use timestampMs.", location=f"locations[{i}]",
                            before=rec["timestampMs"], after=iso,
                            op={"kind": "set", "path": [name, i, "timestamp"], "value": iso}))
    if n_e7 or n_ms:
        plan.findings.append(Finding("schema_normalization", "info",
                                     f"{n_e7} E7 coordinates and {n_ms} epoch-ms timestamps normalised."))


# ============================================================ pass 4
def _swap_candidate(value: Any) -> Optional[Tuple[float, float]]:
    if isinstance(value, dict):
        value = value.get("latLng") or value.get("LatLng") or value.get("point")
    if not isinstance(value, str):
        return None
    parts = value.replace("geo:", "").replace("°", "").replace(" ", "").split(",")
    try:
        a, b = float(parts[0]), float(parts[1])
    except (ValueError, IndexError):
        return None
    if abs(a) > 90 and abs(a) <= 180 and abs(b) <= 85.05:
        return b, a
    return None


def coordinate_repair(doc: Any, plan: RepairPlan) -> None:
    def check(name, i, path_desc, getter_path, value, set_path=None):
        set_path = set_path or getter_path
        c, reason = parse_coordinate_detailed(value)
        if c is not None:
            if abs(c[0]) < 1e-6 and abs(c[1]) < 1e-6:
                plan.actions.append(RepairAction(
                    f"coord-null-island-{name}-{i}-{path_desc}", "coordinate_repair", Confidence.MEDIUM,
                    "Remove (0°, 0°) 'Null Island' coordinate",
                    "Exactly zero latitude and longitude is a classic placeholder for a failed fix.",
                    location=f"{name}[{i}].{path_desc}", before=value,
                    op={"kind": "delete", "path": getter_path}))
            return
        swapped = _swap_candidate(value)
        if swapped is not None:
            plan.actions.append(RepairAction(
                f"coord-swap-{name}-{i}-{path_desc}", "coordinate_repair", Confidence.LOW,
                "Swap latitude and longitude",
                "Latitude is out of range but the swapped pair is valid. Without neighbouring "
                "evidence this is only a guess.",
                location=f"{name}[{i}].{path_desc}", before=value, after=_fmt_coord(*swapped),
                op={"kind": "set", "path": set_path, "value": _fmt_coord(*swapped)}))
        elif reason in ("malformed_coordinate", "coordinate_out_of_range"):
            plan.actions.append(RepairAction(
                f"coord-invalid-{name}-{i}-{path_desc}", "coordinate_repair", Confidence.MEDIUM,
                "Drop unusable coordinate", f"Coordinate is {reason.replace('_', ' ')}.",
                location=f"{name}[{i}].{path_desc}", before=value,
                op={"kind": "delete", "path": getter_path}))

    for name, items in _containers(doc):
        for i, seg in enumerate(items):
            if not isinstance(seg, dict):
                continue
            if name in ("semanticSegments", "$root"):
                for j, pp in enumerate(seg.get("timelinePath") or []):
                    if isinstance(pp, dict) and "point" in pp:
                        check(name, i, f"timelinePath[{j}]", [name, i, "timelinePath", j], pp["point"],
                              [name, i, "timelinePath", j, "point"])
                cand = (seg.get("visit") or {}).get("topCandidate") if isinstance(seg.get("visit"), dict) else None
                if isinstance(cand, dict) and "placeLocation" in cand:
                    pl = cand["placeLocation"]
                    sp = [name, i, "visit", "topCandidate", "placeLocation"]
                    if isinstance(pl, dict):
                        sp.append("latLng" if "latLng" in pl else "LatLng" if "LatLng" in pl else "point")
                    check(name, i, "visit", [name, i], pl, sp)
            elif name == "rawSignals":
                pos = seg.get("position")
                if isinstance(pos, dict) and ("LatLng" in pos or "latLng" in pos):
                    key = "LatLng" if "LatLng" in pos else "latLng"
                    check(name, i, "position", [name, i], pos.get(key), [name, i, "position", key])
            elif name == "locations":
                c, reason = parse_e7_pair(seg)
                if c is None and reason != "missing_coordinate":
                    plan.actions.append(RepairAction(
                        f"coord-invalid-{name}-{i}", "coordinate_repair", Confidence.MEDIUM,
                        "Drop record with unusable coordinate", reason.replace("_", " "),
                        location=f"{name}[{i}]", op={"kind": "delete", "path": [name, i]}))
                elif c is not None and abs(c[0]) < 1e-6 and abs(c[1]) < 1e-6:
                    plan.actions.append(RepairAction(
                        f"coord-null-island-{name}-{i}", "coordinate_repair", Confidence.MEDIUM,
                        "Remove (0°, 0°) 'Null Island' record", "Placeholder coordinate.",
                        location=f"{name}[{i}]", op={"kind": "delete", "path": [name, i]}))


# ============================================================ pass 5
def timestamp_repair(doc: Any, plan: RepairPlan) -> None:
    now = time.time() + 86400
    for name, items in _containers(doc):
        for i, seg in enumerate(items):
            if not isinstance(seg, dict):
                continue
            if name in ("semanticSegments", "$root"):
                s_raw, e_raw = seg.get("startTime"), seg.get("endTime")
                s, e = parse_instant(s_raw), parse_instant(e_raw)
                if s_raw is not None and s is None:
                    plan.actions.append(RepairAction(
                        f"ts-unparseable-{name}-{i}", "timestamp_repair", Confidence.MEDIUM,
                        "Drop segment with unparseable startTime", f"startTime={s_raw!r}",
                        location=f"{name}[{i}]", before=s_raw, op={"kind": "delete", "path": [name, i]}))
                    continue
                if s and (s[0] < EARLIEST_PLAUSIBLE or s[0] > now):
                    plan.actions.append(RepairAction(
                        f"ts-implausible-{name}-{i}", "timestamp_repair", Confidence.MEDIUM,
                        "Drop segment with implausible date",
                        "Timeline did not exist before 2005 and cannot contain future dates.",
                        location=f"{name}[{i}]", before=s_raw, op={"kind": "delete", "path": [name, i]}))
                    continue
                if s and e and not s[2] and not e[2] and e[0] < s[0]:
                    plan.actions.append(RepairAction(
                        f"ts-swap-{name}-{i}", "timestamp_repair", Confidence.LOW,
                        "Swap startTime and endTime", "endTime precedes startTime.",
                        location=f"{name}[{i}]", before=[s_raw, e_raw], after=[e_raw, s_raw],
                        op={"kind": "swap_times", "path": [name, i]}))
            elif name == "rawSignals":
                pos = seg.get("position")
                if isinstance(pos, dict) and pos.get("timestamp") is not None and parse_instant(pos["timestamp"]) is None:
                    plan.actions.append(RepairAction(
                        f"ts-unparseable-{name}-{i}", "timestamp_repair", Confidence.MEDIUM,
                        "Drop raw signal with unparseable timestamp", repr(pos["timestamp"]),
                        location=f"{name}[{i}]", op={"kind": "delete", "path": [name, i]}))


# ============================================================ pass 6
def ordering_repair(doc: Any, plan: RepairPlan) -> None:
    for name, items in _containers(doc):
        if name not in ("semanticSegments", "$root", "timelineObjects"):
            continue
        anchors = []
        for i, seg in enumerate(items):
            if isinstance(seg, dict):
                st = parse_instant(seg.get("startTime"))
                if st is not None:
                    anchors.append((i, st))
        if len(anchors) < 3 or any(a[1][2] for a in anchors):
            continue
        ts = [a[1][0] for a in anchors]
        asc = sum(1 for a, b in zip(ts, ts[1:]) if b >= a)
        desc = len(ts) - 1 - asc
        if desc > asc and ts[-1] < ts[0]:
            plan.actions.append(RepairAction(
                f"order-reverse-{name}", "ordering_repair", Confidence.MEDIUM,
                "Reverse newest-first export order",
                f"{desc} of {len(ts) - 1} consecutive segments go backwards in time.",
                location=name, op={"kind": "reverse", "path": [name]}))
        elif 0 < desc and desc < asc:
            plan.actions.append(RepairAction(
                f"order-sort-{name}", "ordering_repair", Confidence.MEDIUM,
                "Sort segments chronologically",
                f"{desc} segments are out of chronological order (all timestamps carry a timezone, "
                "so sorting is unambiguous).",
                location=name, count=desc, op={"kind": "sort_by_start", "path": [name]}))


# ============================================================ pass 7
def duplicate_cleanup(doc: Any, plan: RepairPlan) -> None:
    for name, items in _containers(doc):
        seen: Dict[str, int] = {}
        dups = []
        for i, el in enumerate(items):
            key = json.dumps(el, sort_keys=True, ensure_ascii=False)
            if key in seen:
                dups.append(i)
            else:
                seen[key] = i
        if dups:
            plan.actions.append(RepairAction(
                f"dup-{name}", "duplicate_cleanup", Confidence.HIGH,
                "Remove exact duplicate records",
                f"{len(dups)} records in '{name}' are byte-for-byte identical to an earlier record.",
                location=name, count=len(dups),
                op={"kind": "delete_many", "container": name, "indices": dups}))


# ============================================================ pass 8
def _point_refs(doc: Any):
    """(t, lat, lon, delete_path, label) for every timed coordinate."""
    refs = []
    for name, items in _containers(doc):
        for i, seg in enumerate(items):
            if not isinstance(seg, dict):
                continue
            if name in ("semanticSegments", "$root"):
                s = parse_instant(seg.get("startTime"))
                for j, pp in enumerate(seg.get("timelinePath") or []):
                    if not isinstance(pp, dict):
                        continue
                    t = parse_instant(pp.get("time"))
                    if t is None and s is not None:
                        try:
                            t = (s[0] + int(pp.get("durationMinutesOffsetFromStartTime")) * 60, s[1], s[2])
                        except (TypeError, ValueError):
                            t = None
                    c = parse_coordinate_detailed(pp.get("point"))[0]
                    if t and c and not t[2]:
                        refs.append((t[0], c[0], c[1], [name, i, "timelinePath", j],
                                     f"{name}[{i}].timelinePath[{j}]"))
                v = seg.get("visit")
                cand = v.get("topCandidate") if isinstance(v, dict) else None
                if isinstance(cand, dict) and s and not s[2]:
                    c = parse_coordinate_detailed(cand.get("placeLocation"))[0]
                    if c:
                        refs.append((s[0], c[0], c[1], [name, i], f"{name}[{i}] (visit)"))
            elif name == "rawSignals":
                pos = seg.get("position")
                if isinstance(pos, dict):
                    t = parse_instant(pos.get("timestamp"))
                    c = parse_coordinate_detailed(pos.get("LatLng") or pos.get("latLng"))[0]
                    if t and c and not t[2]:
                        refs.append((t[0], c[0], c[1], [name, i], f"{name}[{i}]"))
            elif name == "locations":
                t = parse_instant(seg.get("timestamp")) or parse_instant(seg.get("timestampMs"))
                c = parse_e7_pair(seg)[0]
                if t and c:
                    refs.append((t[0], c[0], c[1], [name, i], f"{name}[{i}]"))
    refs.sort(key=lambda r: r[0])
    return refs


def outlier_analysis(doc: Any, plan: RepairPlan) -> None:
    refs = _point_refs(doc)
    if len(refs) < 3:
        return
    t = [r[0] for r in refs]
    lat = [r[1] for r in refs]
    lon = [r[2] for r in refs]
    exc = set(find_excursions(t, lat, lon))
    for k in sorted(exc):
        r = refs[k]
        plan.actions.append(RepairAction(
            f"outlier-excursion-{r[4]}", "outlier_analysis", Confidence.MEDIUM,
            "Remove GPS teleport excursion",
            "The point jumps ≥500 km away at >1300 km/h and returns within 12 h — "
            "physically implausible for real travel.",
            location=r[4], before=_fmt_coord(r[1], r[2]), op={"kind": "delete", "path": r[3]}))
    for idx, speed, z in speed_mad_scores(t, lat, lon):
        if idx in exc or z < 8.0 or speed < 1100:
            continue
        r = refs[idx]
        plan.actions.append(RepairAction(
            f"outlier-speed-{r[4]}", "outlier_analysis", Confidence.LOW,
            "Remove statistically unusual hop",
            f"Implied speed {speed:,.0f} km/h (robust z = {z:.1f}). Could be a real flight with a "
            "sparse record, so this is not removed unless you accept it.",
            location=r[4], before=_fmt_coord(r[1], r[2]), op={"kind": "delete", "path": r[3]}))


# ============================================================ pass 9
def semantic_reconstruction(doc: Any, plan: RepairPlan) -> None:
    for name, items in _containers(doc):
        if name not in ("semanticSegments", "$root"):
            continue
        for i, seg in enumerate(items):
            if not isinstance(seg, dict):
                continue
            path = [pp for pp in (seg.get("timelinePath") or []) if isinstance(pp, dict)]
            if "startTime" not in seg and path:
                first = parse_instant(path[0].get("time"))
                if first is not None:
                    plan.actions.append(RepairAction(
                        f"sem-start-{name}-{i}", "semantic_reconstruction", Confidence.MEDIUM,
                        "Restore missing startTime from first path point",
                        "The segment has timed path points but no startTime.",
                        location=f"{name}[{i}]", after=path[0]["time"],
                        op={"kind": "set", "path": [name, i, "startTime"], "value": path[0]["time"]}))
            act = seg.get("activity")
            if isinstance(act, dict) and path:
                for end_key, pp in (("start", path[0]), ("end", path[-1])):
                    if parse_coordinate_detailed(act.get(end_key))[0] is None:
                        c = parse_coordinate_detailed(pp.get("point"))[0]
                        if c is not None:
                            plan.actions.append(RepairAction(
                                f"sem-act-{end_key}-{name}-{i}", "semantic_reconstruction",
                                Confidence.MEDIUM, f"Restore activity {end_key} from its path",
                                f"activity.{end_key} is missing; the {'first' if end_key == 'start' else 'last'} "
                                "path point is used.",
                                location=f"{name}[{i}].activity.{end_key}", after=_fmt_coord(*c),
                                op={"kind": "set", "path": [name, i, "activity", end_key],
                                    "value": _fmt_coord(*c)}))


# ============================================================ apply
def analyze(path: str | Path, progress: Optional[Callable[[str], None]] = None) -> RepairPlan:
    p = Path(path)
    plan = RepairPlan(source=str(p), source_bytes=p.stat().st_size)

    def timed(name, fn, *a):
        if progress:
            progress(name)
        t0 = time.perf_counter()
        r = fn(*a)
        plan.pass_timings[name] = time.perf_counter() - t0
        return r

    text = timed("forensic_scan", forensic_scan, p, plan)
    doc = timed("structural_recovery", structural_recovery, text, plan)
    plan.document = doc
    if doc is None:
        return plan
    for name, fn in (("schema_normalization", schema_normalization),
                     ("coordinate_repair", coordinate_repair),
                     ("timestamp_repair", timestamp_repair),
                     ("ordering_repair", ordering_repair),
                     ("duplicate_cleanup", duplicate_cleanup),
                     ("outlier_analysis", outlier_analysis),
                     ("semantic_reconstruction", semantic_reconstruction)):
        timed(name, fn, doc, plan)
    # Deduplicate action ids (a coordinate may be flagged twice by different passes)
    seen = set()
    uniq = []
    for a in plan.actions:
        if a.id in seen:
            continue
        seen.add(a.id)
        uniq.append(a)
    plan.actions = uniq
    return plan


def _navigate(doc: Any, path: list):
    obj = _get_container(doc, path[0])
    for k in path[1:-1]:
        obj = obj[k]
    return obj, path[-1]


def _apply_ops(doc: Any, actions: List[RepairAction]) -> Any:
    """Apply accepted operations to a deep copy. Indices refer to the original document:
    deletions are tombstoned first and swept at the end, reorderings run last."""
    doc = copy.deepcopy(doc)
    deletes: set = set()
    for a in actions:
        op = a.op
        kind = op.get("kind")
        try:
            if kind == "set":
                parent, key = _navigate(doc, op["path"])
                parent[key] = op["value"]
            elif kind == "swap_times":
                seg = _get_container(doc, op["path"][0])[op["path"][1]]
                seg["startTime"], seg["endTime"] = seg.get("endTime"), seg.get("startTime")
            elif kind == "delete":
                deletes.add(tuple(op["path"]))
            elif kind == "delete_many":
                deletes.update((op["container"], i) for i in op["indices"])
        except (KeyError, IndexError, TypeError):
            continue
    for path in deletes:
        try:
            parent, key = _navigate(doc, list(path))
        except (KeyError, IndexError, TypeError):
            continue
        if isinstance(parent, list) and isinstance(key, int) and 0 <= key < len(parent):
            parent[key] = _TOMBSTONE
        elif isinstance(parent, dict):
            parent.pop(key, None)
    _sweep(doc)
    for a in actions:
        kind = a.op.get("kind")
        if kind == "reverse":
            _get_container(doc, a.op["path"][0]).reverse()
        elif kind == "sort_by_start":
            _get_container(doc, a.op["path"][0]).sort(
                key=lambda s: (parse_instant(s.get("startTime")) or (float("inf"),))[0]
                if isinstance(s, dict) else float("inf"))
    return doc


class _Tomb:
    pass


_TOMBSTONE = _Tomb()


def _sweep(obj: Any) -> Any:
    if isinstance(obj, list):
        obj[:] = [_sweep(x) for x in obj if x is not _TOMBSTONE]
    elif isinstance(obj, dict):
        for k in list(obj):
            obj[k] = _sweep(obj[k])
    return obj


@dataclass
class RepairResult:
    fixed_path: Path
    report_json: Path
    report_html: Path
    log_path: Path
    applied: List[RepairAction]
    rejected: List[RepairAction]
    validation: Dict[str, Any]


def apply_plan(plan: RepairPlan, *, accept_ids: Optional[set] = None, reject_ids: Optional[set] = None,
               accept_low: bool = False, output_dir: Optional[Path] = None) -> RepairResult:
    if plan.document is None:
        raise ImportFailedError("Nothing to repair: no Timeline records could be recovered.")
    accept_ids = accept_ids or set()
    reject_ids = reject_ids or set()
    applied, rejected = [], []
    for a in plan.actions:
        ok = (a.id in accept_ids) or (a.default_accept and a.id not in reject_ids) \
            or (accept_low and a.confidence == Confidence.LOW and a.id not in reject_ids)
        a.applied = bool(ok)
        (applied if ok else rejected).append(a)
    t0 = time.perf_counter()
    fixed = _apply_ops(plan.document, applied)
    src = Path(plan.source)
    out_dir = Path(output_dir) if output_dir else src.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = src.name[:-5] if src.name.lower().endswith(".json") else src.stem
    fixed_path = out_dir / f"{stem}.fixed.json"
    if fixed_path.resolve() == src.resolve():
        raise ImportFailedError("Refusing to overwrite the source file.")
    tmp = fixed_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(fixed, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(fixed_path)
    plan.pass_timings["apply"] = time.perf_counter() - t0

    # pass 10 — validation
    t0 = time.perf_counter()
    validation: Dict[str, Any] = {}
    try:
        tl = load_timeline(fixed_path, compute_sha=False, limits=ImportLimits())
        validation = {"ok": True, "format": tl.diagnostics.detected_format,
                      "semantic_points": len(tl.semantic), "raw_points": len(tl.raw),
                      "skipped": tl.diagnostics.skipped}
        if plan.structurally_valid:
            try:
                before = load_timeline(src, compute_sha=False)
                validation["points_before"] = len(before.semantic) + len(before.raw)
            except Exception as e:  # noqa: BLE001
                validation["points_before"] = None
                validation["before_error"] = str(e)
        else:
            validation["points_before"] = None
    except Exception as e:  # noqa: BLE001
        validation = {"ok": False, "error": str(e)}
    plan.pass_timings["validation"] = time.perf_counter() - t0

    report = build_report(plan, applied, rejected, validation, fixed_path)
    rj = out_dir / f"{stem}.repair-report.json"
    rj.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    rh = out_dir / f"{stem}.repair-report.html"
    rh.write_text(render_html_report(report), encoding="utf-8")
    lg = out_dir / f"{stem}.repair-log.txt"
    lg.write_text(render_text_log(report), encoding="utf-8")
    return RepairResult(fixed_path, rj, rh, lg, applied, rejected, validation)


def build_report(plan, applied, rejected, validation, fixed_path) -> dict:
    return {
        "tool": f"TimelinerX {__version__}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": plan.source, "source_bytes": plan.source_bytes, "encoding": plan.encoding,
        "structurally_valid": plan.structurally_valid, "fixed_file": str(fixed_path),
        "summary": {"proposed": plan.summary(), "applied": len(applied), "rejected": len(rejected)},
        "passes": PASSES, "pass_timings_s": plan.pass_timings,
        "findings": [asdict(f) for f in plan.findings],
        "actions": [a.to_dict() for a in plan.actions],
        "validation": validation,
        "note": "Coordinates of individual actions are included because this report stays on your "
                "computer. Do not share it if you consider them private.",
    }


def render_text_log(report: dict) -> str:
    lines = [f"{report['tool']} — repair log", f"Generated: {report['generated_at']}",
             f"Source: {report['source']} ({report['source_bytes']:,} bytes, {report['encoding']})",
             f"Fixed file: {report['fixed_file']}", ""]
    for f in report["findings"]:
        lines.append(f"[{f['severity'].upper():7}] {f['pass_name']}: {f['message']}")
    lines.append("")
    for a in report["actions"]:
        mark = "APPLIED " if a["applied"] else "SKIPPED "
        lines.append(f"{mark}[{a['confidence']:6}] {a['pass_name']}: {a['title']} "
                     f"@ {a['location'] or '-'} — {a['detail']}")
    lines.append("")
    lines.append(f"Validation: {json.dumps(report['validation'], default=str)}")
    return "\n".join(lines) + "\n"


def render_html_report(report: dict) -> str:
    e = html.escape
    rows = []
    for a in report["actions"]:
        rows.append(
            f"<tr class='{a['confidence'].lower()}'><td><span class='badge {a['confidence'].lower()}'>"
            f"{e(a['confidence'])}</span></td><td>{'✔ applied' if a['applied'] else '✖ not applied'}</td>"
            f"<td>{e(a['pass_name'])}</td><td><b>{e(a['title'])}</b><br><small>{e(a['detail'])}</small></td>"
            f"<td><code>{e(a['location'] or '')}</code></td>"
            f"<td><code>{e(str(a['before']) if a['before'] is not None else '')}</code></td>"
            f"<td><code>{e(str(a['after']) if a['after'] is not None else '')}</code></td></tr>")
    findings = "".join(f"<li class='{e(f['severity'])}'><b>{e(f['pass_name'])}</b>: {e(f['message'])}</li>"
                       for f in report["findings"])
    v = report["validation"]
    s = report["summary"]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Timeline repair report</title><style>
body{{font:14px/1.5 "Segoe UI",system-ui,sans-serif;margin:32px;color:#1c2330;background:#f6f7f9}}
h1{{font-size:22px;margin:0 0 4px}} .meta{{color:#5b6475;margin-bottom:24px}}
.cards{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}}
.card{{background:#fff;border:1px solid #dde1e8;border-radius:8px;padding:12px 16px;min-width:140px}}
.card b{{display:block;font-size:22px}} table{{border-collapse:collapse;width:100%;background:#fff}}
td,th{{border-bottom:1px solid #e6e9ee;padding:8px;text-align:left;vertical-align:top}}
th{{background:#eef1f5;font-weight:600}} code{{font-size:12px;word-break:break-all}}
.badge{{padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700}}
.badge.high{{background:#dff3e4;color:#11672b}} .badge.medium{{background:#fff1d6;color:#7a4b00}}
.badge.low{{background:#fde2e2;color:#8a1c1c}} li.error{{color:#8a1c1c}} li.warning{{color:#7a4b00}}
</style></head><body>
<h1>Timeline repair report</h1>
<div class="meta">{e(report['tool'])} · {e(report['generated_at'])}<br>Source: <code>{e(report['source'])}</code>
({report['source_bytes']:,} bytes) → <code>{e(report['fixed_file'])}</code><br>
The original file was not modified.</div>
<div class="cards">
<div class="card">HIGH proposed<b>{s['proposed']['HIGH']}</b></div>
<div class="card">MEDIUM proposed<b>{s['proposed']['MEDIUM']}</b></div>
<div class="card">LOW proposed<b>{s['proposed']['LOW']}</b></div>
<div class="card">Applied<b>{s['applied']}</b></div>
<div class="card">Validation<b>{'passed' if v.get('ok') else 'FAILED'}</b></div></div>
<h2>Findings</h2><ul>{findings or '<li>No findings.</li>'}</ul>
<h2>Actions</h2><table><tr><th>Confidence</th><th>Status</th><th>Pass</th><th>Action</th><th>Location</th>
<th>Before</th><th>After</th></tr>{''.join(rows) or '<tr><td colspan=7>No repairs were needed.</td></tr>'}</table>
<h2>Validation</h2><pre>{e(json.dumps(v, indent=2, default=str))}</pre>
<p><small>{e(report['note'])}</small></p></body></html>"""
