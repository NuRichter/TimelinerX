"""Engine 2 (tlx-2.x): transport modes, flight arcs, zoom styles, long-trip pacing, smoothness."""

import json
import math

import numpy as np
import pytest

from timelinerx.camera.planner import (CAMERA_MODES, CameraConfig, LONG_TRIP_PACING, pace_of, plan_frames,
                                       recommend_duration)
from timelinerx.journeys.journey import JourneyConfig, build_journey, journey_from_columns
from timelinerx.timeline.model import PointColumns, PointKind
from timelinerx.timeline.modes import Mode, infer_flight, mode_from_type
from timelinerx.timeline.parser import load_timeline


def _seg_visit(t0, t1, lat, lon):
    return {"startTime": t0, "endTime": t1, "visit": {"topCandidate": {"placeLocation": {"latLng": f"{lat}°, {lon}°"}}}}


def _seg_act(t0, t1, a, b, typ):
    return {"startTime": t0, "endTime": t1, "activity": {"start": {"latLng": f"{a[0]}°, {a[1]}°"},
                                                          "end": {"latLng": f"{b[0]}°, {b[1]}°"},
                                                          "topCandidate": {"type": typ}}}


@pytest.fixture
def trip_file(tmp_path):
    """Jakarta local wandering → flight to Manado → local → flight back; motorbike rides."""
    segs = []
    day = 1
    jkt, mdo = (-6.2, 106.8), (1.49, 124.84)

    def ts(d, h, m=0):
        return f"2026-01-{d:02d}T{h:02d}:{m:02d}:00+07:00"
    rng = np.random.default_rng(3)
    for base, d0 in ((jkt, 1), (mdo, 6), (jkt, 11)):
        for d in range(d0, d0 + 4):
            p0 = base
            for h in range(8, 20, 2):
                p1 = (base[0] + rng.normal(0, 0.08), base[1] + rng.normal(0, 0.08))
                segs.append(_seg_act(ts(d, h), ts(d, h, 40), p0, p1, "MOTORCYCLING"))
                segs.append(_seg_visit(ts(d, h, 40), ts(d, h + 2), *p1))
                p0 = p1
    segs.append(_seg_act(ts(5, 9), ts(5, 12), jkt, mdo, "FLYING"))
    segs.append(_seg_act(ts(10, 9), ts(10, 12), mdo, jkt, "FLYING"))
    segs.sort(key=lambda s: s["startTime"])
    p = tmp_path / "Timeline.json"
    p.write_text(json.dumps({"semanticSegments": segs}), encoding="utf-8")
    return p


def test_mode_table_and_flight_inference():
    assert mode_from_type("FLYING") == Mode.FLIGHT
    assert mode_from_type("motorcycling") == Mode.CYCLE
    assert mode_from_type("IN_PASSENGER_VEHICLE") == Mode.ROAD
    assert mode_from_type("IN_TRAIN") == Mode.RAIL
    assert mode_from_type("IN_FERRY") == Mode.WATER
    assert mode_from_type("SOMETHING_NEW") == Mode.UNKNOWN
    assert infer_flight(900, 3600 * 1.5)          # 600 km/h
    assert not infer_flight(900, 3600 * 12)       # 75 km/h: a long drive, not a flight
    assert not infer_flight(100, 600)             # too short to call it


def test_parser_keeps_transport_modes(trip_file):
    tl = load_timeline(trip_file, compute_sha=False)
    modes = set(tl.semantic.mode.tolist())
    assert Mode.FLIGHT in modes and Mode.CYCLE in modes
    j = build_journey(tl, JourneyConfig())
    assert j.stats["flights"] == 2 and len(j.arcs) == 2
    assert j.trip_count >= 2
    # flights are long trips even with the most conservative detection
    jc = build_journey(tl, JourneyConfig(trip_detection="conservative"))
    assert jc.trip_count >= 2


def test_flight_arc_geometry(trip_file):
    j = build_journey(load_timeline(trip_file, compute_sha=False), JourneyConfig())
    i = next(iter(j.arcs))
    a, b = j.cum_km[i - 1], j.cum_km[i]
    p0 = j.xy_at(a + 1e-9)
    p1 = j.xy_at(b)
    mid = j.xy_at((a + b) / 2)
    chord_mid_y = (p0[1] + p1[1]) / 2
    assert mid[1] > chord_mid_y, "arcs bow toward the top of the map"
    # arc-length parameterisation: equal distance steps → equal on-map steps (±5 %)
    pts = np.asarray([j.xy_at(a + (b - a) * k / 40) for k in range(41)])
    steps = np.hypot(*np.diff(pts, axis=0).T)
    assert steps.max() / steps.min() < 1.05
    assert j.mode_at((a + b) / 2) == Mode.FLIGHT


def test_zoom_styles_are_the_four_published_ones():
    assert list(CAMERA_MODES) == ["fixed", "balanced", "active", "close_up"]


def test_legacy_project_options_migrate(tmp_path, fix):
    from timelinerx.projects.project import Project
    d = Project().to_dict()
    d["format"] = "nurichter-project"
    d["camera"]["mode"] = "dynamic"
    d["camera"]["compression"] = "stronger"
    p = Project.from_dict(d)
    assert p.camera.mode == "active" and p.camera.compression == "fastest"
    assert not p.validate() or all("Zoom" not in e for e in p.validate())


def test_long_trip_pacing_is_monotone(trip_file):
    j = build_journey(load_timeline(trip_file, compute_sha=False), JourneyConfig())
    share = []
    for pc in LONG_TRIP_PACING:
        plan = plan_frames(j, CameraConfig(mode="active", compression=pc), width=640, height=360, fps=30,
                           duration_s=40)
        inside = np.zeros(plan.frame_count, bool)
        for a, b, t in j.legs:
            if t:
                inside |= (plan.marker_d > a) & (plan.marker_d < b)
        share.append((inside & (plan.phase == 1)).sum())
    assert share == sorted(share, reverse=True), share
    assert share[0] > share[-1]


def test_marker_pace_is_even_across_styles(trip_file):
    """Screen-speed equalisation: the on-screen marker speed stays within a small band."""
    j = build_journey(load_timeline(trip_file, compute_sha=False), JourneyConfig())
    for m in CAMERA_MODES:
        plan = plan_frames(j, CameraConfig(mode=m), width=960, height=540, fps=30, duration_s=40)
        jf = np.nonzero(plan.phase == 1)[0]
        sy = plan.span_y[jf]
        v = np.hypot(np.diff(plan.marker_x[jf]) / (sy[1:] * plan.aspect), np.diff(plan.marker_y[jf]) / sy[1:])
        moving = v[v > 1e-6]
        ratio = np.percentile(moving, 99) / np.median(moving)
        assert ratio < 4.5, (m, ratio)
        # the marker is always on screen
        ox = np.abs(plan.marker_x - plan.cx) / (plan.span_y * plan.aspect)
        oy = np.abs(plan.marker_y - plan.cy) / plan.span_y
        assert (ox[jf] <= 0.5).all() and (oy[jf] <= 0.5).all()


def test_duration_recommendation_is_consistent(trip_file):
    j = build_journey(load_timeline(trip_file, compute_sha=False), JourneyConfig())
    cfg = CameraConfig(mode="active")
    r = recommend_duration(j, cfg, 1920, 1080)
    assert r["brisk_s"] < r["comfortable_s"]
    plan = plan_frames(j, cfg, width=1920, height=1080, fps=30, duration_s=r["comfortable_s"])
    assert pace_of(plan) == pytest.approx(0.30, rel=0.35)


def test_renderer_draws_plane_during_flight(trip_file, tmp_path):
    from timelinerx.pipeline.render_job import build_renderer, camera_config
    from timelinerx.projects.project import Project
    j = build_journey(load_timeline(trip_file, compute_sha=False), JourneyConfig())
    proj = Project(name="T")
    proj.visual.map_provider = "plain"
    plan = plan_frames(j, camera_config(proj), width=640, height=360, fps=30, duration_s=20)
    r, _, _ = build_renderer(proj, j, plan, allow_placeholder=True)
    i = next(iter(j.arcs))
    mid = 0.5 * (j.cum_km[i - 1] + j.cum_km[i])
    f = int(np.argmin(np.abs(plan.marker_d - mid)))
    w, _ = r._flight_blend(float(plan.marker_d[f]))
    assert w > 0.9
    img = r.render(f)
    assert img.width() == 640
