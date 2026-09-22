"""Generate the synthetic regression fixtures in ``fixtures/``.

Every coordinate here is generated. No real personal Timeline data is used.
Run: python scripts/make_fixtures.py  (deterministic: fixed RNG seed)
"""

from __future__ import annotations

import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "fixtures"
TZ7 = timezone(timedelta(hours=7))


def pt(lat, lon):
    return f"{lat:.7f}°, {lon:.7f}°"


def path_seg(points, t0, step_min=4, tz=TZ7):
    return {"startTime": t0.isoformat(), "endTime": (t0 + timedelta(minutes=step_min * len(points))).isoformat(),
            "timelinePath": [{"point": pt(a, b), "time": (t0 + timedelta(minutes=step_min * i)).isoformat()}
                             for i, (a, b) in enumerate(points)]}


def visit(lat, lon, t0, hours=6):
    return {"startTime": t0.isoformat(), "endTime": (t0 + timedelta(hours=hours)).isoformat(),
            "visit": {"topCandidate": {"placeLocation": {"latLng": pt(lat, lon)}}}}


def activity(a, b, t0, hours, kind="FLYING"):
    return {"startTime": t0.isoformat(), "endTime": (t0 + timedelta(hours=hours)).isoformat(),
            "activity": {"start": {"latLng": pt(*a)}, "end": {"latLng": pt(*b)}, "topCandidate": {"type": kind}}}


def commute_days(center, days, t, rng, n=30, scale=0.03):
    segs = []
    for d in range(days):
        pts = [(center[0] + scale * math.sin(i / 6 + d) + rng.uniform(-1e-4, 1e-4),
                center[1] + scale * 1.3 * math.cos(i / 7 + d)) for i in range(n)]
        segs.append(path_seg(pts, t))
        segs.append(visit(*pts[-1], t + timedelta(hours=2)))
        t += timedelta(days=1)
    return segs, t


def small(rng):
    t = datetime(2024, 5, 1, 8, tzinfo=TZ7)
    segs, t = commute_days((-7.2575, 112.7521), 3, t, rng)
    return {"semanticSegments": segs}


def standard_route(rng):
    """The standard test route used for camera verification and visual regression."""
    t = datetime(2024, 5, 1, 8, tzinfo=TZ7)
    sby, jkt, tyo = (-7.2575, 112.7521), (-6.2088, 106.8456), (35.6762, 139.6503)
    segs = []
    s, t = commute_days(sby, 5, t, rng)
    segs += s
    segs.append(activity(sby, jkt, t, 1.5))
    t += timedelta(hours=4)
    s, t = commute_days(jkt, 4, t, rng)
    segs += s
    segs.append(activity(jkt, tyo, t, 7))
    t += timedelta(hours=10)
    s, t = commute_days(tyo, 4, t, rng)
    segs += s
    segs.append(activity(tyo, sby, t, 8))
    t += timedelta(hours=10)
    s, t = commute_days(sby, 2, t, rng)
    segs += s
    return {"semanticSegments": segs}


def sparse(rng):
    t = datetime(2023, 1, 1, 9, tzinfo=timezone.utc)
    return {"semanticSegments": [visit(-6.2 + i * 0.4, 106.8 + i * 0.9, t + timedelta(days=17 * i)) for i in range(8)]}


def dense(rng):
    t = datetime(2024, 2, 1, 6, tzinfo=TZ7)
    segs = []
    for d in range(10):
        pts = [(-7.25 + 0.05 * math.sin(i / 40 + d), 112.75 + 0.06 * math.cos(i / 55)) for i in range(600)]
        segs.append(path_seg(pts, t + timedelta(days=d), step_min=1))
    return {"semanticSegments": segs}


def dateline(rng):
    t = datetime(2024, 7, 1, tzinfo=timezone.utc)
    return {"semanticSegments": [visit(35.55, 139.78, t), activity((35.55, 139.78), (21.32, -157.92), t + timedelta(hours=8), 7),
                                 visit(21.32, -157.92, t + timedelta(hours=16)),
                                 activity((21.32, -157.92), (33.94, -118.40), t + timedelta(days=3), 5.5),
                                 visit(33.94, -118.40, t + timedelta(days=3, hours=7))]}


def timezone_change(rng):
    segs = []
    for i, (lat, lon, h) in enumerate([(-6.2, 106.8, 7), (-8.65, 115.2, 8), (1.35, 103.8, 8), (13.75, 100.5, 7)]):
        tz = timezone(timedelta(hours=h))
        segs.append(visit(lat, lon, datetime(2024, 3, 1 + i * 2, 10, tzinfo=tz)))
    return {"semanticSegments": segs}


def duplicates(rng):
    d = small(rng)
    d["semanticSegments"] += d["semanticSegments"][:4]
    return d


def outliers(rng):
    t = datetime(2024, 6, 1, 8, tzinfo=timezone.utc)
    pts = [(-7.25 + i * 0.001, 112.75 + i * 0.001) for i in range(10)]
    segs = [visit(a, b, t + timedelta(minutes=20 * i), 0.3) for i, (a, b) in enumerate(pts)]
    segs.insert(5, visit(48.85, 2.35, t + timedelta(minutes=95), 0.1))  # Paris teleport in 10 min
    return {"semanticSegments": segs}


def missing_values(rng):
    t = datetime(2024, 4, 1, 8, tzinfo=timezone.utc)
    return {"semanticSegments": [
        visit(-7.25, 112.75, t),
        {"startTime": (t + timedelta(hours=2)).isoformat(), "visit": {"topCandidate": {}}},
        {"visit": {"topCandidate": {"placeLocation": {"latLng": pt(-7.3, 112.8)}}}},
        {"startTime": "not-a-date", "visit": {"topCandidate": {"placeLocation": {"latLng": pt(-7.3, 112.8)}}}},
        {"startTime": (t + timedelta(hours=5)).isoformat(), "timelinePath": [{"point": "garbage"}, {"time": t.isoformat()}]},
        visit(-7.28, 112.78, t + timedelta(hours=6)),
    ]}


def semantic_and_raw(rng):
    t = datetime(2024, 8, 1, 8, tzinfo=timezone.utc)
    d = {"semanticSegments": [visit(-7.25, 112.75, t, 1), visit(-7.30, 112.80, t + timedelta(hours=3), 1)],
         "rawSignals": []}
    for i in range(60):
        d["rawSignals"].append({"position": {"LatLng": pt(-7.25 - i * 0.0008, 112.75 + i * 0.0008),
                                             "accuracyMeters": 12, "timestamp": (t + timedelta(minutes=70 + i)).isoformat()}})
    return d


def legacy_records(rng):
    t0 = int(datetime(2019, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)
    return {"locations": [{"latitudeE7": int((-6.2 + 0.01 * i) * 1e7), "longitudeE7": int((106.8 + 0.01 * i) * 1e7),
                           "accuracy": 20, "timestampMs": str(t0 + i * 600000)} for i in range(50)]}


def legacy_semantic(rng):
    t0 = datetime(2019, 10, 1, 8, tzinfo=timezone.utc)
    return {"timelineObjects": [
        {"placeVisit": {"location": {"latitudeE7": -62088000, "longitudeE7": 1068456000},
                        "duration": {"startTimestamp": t0.isoformat().replace("+00:00", "Z"),
                                     "endTimestamp": (t0 + timedelta(hours=2)).isoformat().replace("+00:00", "Z")}}},
        {"activitySegment": {"startLocation": {"latitudeE7": -62088000, "longitudeE7": 1068456000},
                             "endLocation": {"latitudeE7": -69175000, "longitudeE7": 1076191000},
                             "duration": {"startTimestamp": (t0 + timedelta(hours=2)).isoformat(),
                                          "endTimestamp": (t0 + timedelta(hours=5)).isoformat()},
                             "simplifiedRawPath": {"points": [{"latE7": -65000000, "lngE7": 1072000000,
                                                               "timestamp": (t0 + timedelta(hours=3)).isoformat()}]}}},
        {"placeVisit": {"location": {"latitudeE7": -69175000, "longitudeE7": 1076191000},
                        "duration": {"startTimestampMs": str(int((t0 + timedelta(hours=5)).timestamp() * 1000))}}},
    ]}


def ios_array(rng):
    t = datetime(2025, 1, 10, tzinfo=timezone.utc)
    return [{"startTime": t.isoformat(), "endTime": (t + timedelta(hours=1)).isoformat(),
             "activity": {"start": "geo:-7.2575,112.7521", "end": "geo:-7.3,112.73"}},
            {"startTime": (t + timedelta(days=1)).isoformat(),
             "visit": {"topCandidate": {"placeLocation": "geo:-6.2088,106.8456"}}}]


def long_flight(rng):
    t = datetime(2024, 9, 1, tzinfo=timezone.utc)
    return {"semanticSegments": [visit(-6.12, 106.65, t, 2), activity((-6.12, 106.65), (51.47, -0.45), t + timedelta(hours=3), 16),
                                 visit(51.47, -0.45, t + timedelta(hours=20), 2)]}


def descending(rng):
    d = standard_route(rng)
    d["semanticSegments"] = list(reversed(d["semanticSegments"]))
    return d


BUILDERS = {
    "small": small, "standard-route": standard_route, "sparse": sparse, "dense": dense, "dateline": dateline,
    "timezone-change": timezone_change, "duplicates": duplicates, "outliers": outliers,
    "missing-values": missing_values, "semantic-and-raw": semantic_and_raw, "legacy-records": legacy_records,
    "legacy-semantic": legacy_semantic, "ios-array": ios_array, "long-flight": long_flight,
    "descending": descending,
}


def make_large(path: Path, target_mb: float, seed: int = 7) -> Path:
    """Stream a large synthetic Timeline to disk (used by tests and benchmarks)."""
    rng = random.Random(seed)
    t = datetime(2020, 1, 1, 7, tzinfo=TZ7)
    target = int(target_mb * 1024 * 1024)
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"semanticSegments": [')
        first = True
        written = 0
        day = 0
        while written < target:
            c = (-7.2575 + rng.uniform(-0.2, 0.2), 112.7521 + rng.uniform(-0.2, 0.2))
            pts = [(c[0] + 0.02 * math.sin(i / 5 + day), c[1] + 0.02 * math.cos(i / 6)) for i in range(120)]
            for seg in (path_seg(pts, t, 2), visit(*pts[-1], t + timedelta(hours=4))):
                s = json.dumps(seg)
                if not first:
                    f.write(",")
                f.write(s)
                written += len(s) + 1
                first = False
            t += timedelta(hours=12)
            day += 1
        f.write("]}")
    return path


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, fn in BUILDERS.items():
        data = fn(random.Random(42))
        (OUT / f"{name}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    # a truncated and a BOM-prefixed copy for the repair tests
    std = (OUT / "standard-route.json").read_text(encoding="utf-8")
    (OUT / "broken-truncated.json").write_text(std[: int(len(std) * 0.73)], encoding="utf-8")
    (OUT / "broken-bom-trailing-comma.json").write_bytes(
        b"\xef\xbb\xbf" + (OUT / "small.json").read_text(encoding="utf-8").replace("]\n}", ",]\n}").encode())
    print(f"wrote {len(BUILDERS) + 2} fixtures to {OUT}")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "large":
        make_large(Path(sys.argv[2]), float(sys.argv[3]) if len(sys.argv) > 3 else 60)
    else:
        main()
