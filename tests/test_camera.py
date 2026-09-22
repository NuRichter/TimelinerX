"""Section VI.1 verification: camera motion must be free of acceleration spikes."""

import numpy as np
import pytest

from timelinerx.camera.planner import (CAMERA_MODES, CameraConfig, Keyframe, jerk_report, plan_frames,
                                                upstream_style_track_jerk)
from timelinerx.core.geo import latlon_to_meters

# Thresholds in viewport units per frame² at 30 fps. The uncorrected upstream-style path
# measured 0.32–0.38 zoom acceleration on this route; the cinema layer must stay well below.
MAX_PAN_ACCEL = 0.02
MAX_ZOOM_ACCEL = 0.06


@pytest.mark.parametrize("mode", list(CAMERA_MODES))
def test_no_acceleration_spikes(standard_journey, mode):
    fp = plan_frames(standard_journey, CameraConfig(mode=mode), width=1920, height=1080, fps=30, duration_s=30)
    r = jerk_report(fp)
    assert r["max_accel_x_vp"] < MAX_PAN_ACCEL
    assert r["max_accel_y_vp"] < MAX_PAN_ACCEL
    assert r["max_accel_zoom"] < MAX_ZOOM_ACCEL


@pytest.mark.parametrize("mode", ["active", "close_up"])
def test_improves_on_uncorrected_path(standard_journey, mode):
    ours = jerk_report(plan_frames(standard_journey, CameraConfig(mode=mode), width=1920, height=1080, fps=30,
                                   duration_s=30))
    base = upstream_style_track_jerk(standard_journey, mode, 1920, 1080, 30, 30)
    assert ours["max_accel_zoom"] < base["max_accel_zoom"] / 4


@pytest.mark.parametrize("mode", list(CAMERA_MODES))
@pytest.mark.parametrize("dims", [(1920, 1080), (1080, 1920), (1080, 1080)])
def test_marker_stays_in_frame(standard_journey, mode, dims):
    fp = plan_frames(standard_journey, CameraConfig(mode=mode), width=dims[0], height=dims[1], fps=30, duration_s=20)
    j = fp.phase == 1
    ox = np.abs((fp.marker_x - fp.cx) / (fp.span_y * fp.aspect))[j]
    oy = np.abs((fp.marker_y - fp.cy) / fp.span_y)[j]
    assert max(ox.max(), oy.max()) <= 0.43       # inside the frame with margin


def test_thirds_composition_offsets_marker(standard_journey):
    kw = dict(width=1920, height=1080, fps=30, duration_s=30)
    th = plan_frames(standard_journey, CameraConfig(mode="active", composition="thirds"), **kw)
    ce = plan_frames(standard_journey, CameraConfig(mode="active", composition="centered"), **kw)
    def along(fp):
        # signed offset of the marker from centre along the direction of travel (viewport units)
        vx = np.gradient(fp.marker_x) / (fp.span_y * fp.aspect)
        vy = np.gradient(fp.marker_y) / fp.span_y
        sp = np.hypot(vx, vy)
        m = (fp.phase == 1) & (sp > np.percentile(sp[fp.phase == 1], 40))
        ox = (fp.marker_x - fp.cx) / (fp.span_y * fp.aspect)
        oy = (fp.marker_y - fp.cy) / fp.span_y
        return np.median(((ox * vx + oy * vy) / np.maximum(sp, 1e-12))[m])
    a_th, a_ce = along(th), along(ce)
    assert a_th < -0.04          # marker trails the centre: open space ahead (thirds)
    assert a_ce > a_th


def test_deterministic(standard_journey):
    a = plan_frames(standard_journey, CameraConfig(), width=1280, height=720, fps=30, duration_s=10)
    b = plan_frames(standard_journey, CameraConfig(), width=1280, height=720, fps=30, duration_s=10)
    assert np.array_equal(a.cx, b.cx) and np.array_equal(a.span_y, b.span_y)


def test_intro_starts_and_outro_ends_on_overview(standard_journey):
    fp = plan_frames(standard_journey, CameraConfig(), width=1920, height=1080, fps=30, duration_s=20)
    ov = fp.overview
    assert fp.phase[0] == 0 and fp.phase[-1] == 2
    assert fp.span_y[-1] == pytest.approx(ov[2], rel=0.02)
    assert fp.span_y[0] == pytest.approx(ov[2], rel=0.05)


def test_keyframe_is_honoured_with_easing(standard_journey):
    lat, lon = -6.2088, 106.8456
    kf = Keyframe(time_s=10.0, lat=lat, lon=lon, span_km=30.0, hold_s=1.0, ramp_s=1.0)
    fp = plan_frames(standard_journey, CameraConfig(keyframes=[kf]), width=1920, height=1080, fps=30, duration_s=20)
    x, y = latlon_to_meters(lat, lon)
    f = int(10.5 * 30)
    assert abs(fp.cx[f] - x) / fp.span_y[f] < 0.08
    r = jerk_report(fp)
    assert r["max_accel_x_vp"] < 0.05


@pytest.mark.parametrize("name", ["sparse", "dense", "dateline", "long-flight", "outliers", "small"])
def test_edge_routes_plan_cleanly(fix, name):
    from timelinerx.journeys.journey import JourneyConfig, build_journey
    from timelinerx.timeline.parser import load_timeline
    j = build_journey(load_timeline(fix / f"{name}.json", compute_sha=False), JourneyConfig())
    fp = plan_frames(j, CameraConfig(mode="active"), width=1280, height=720, fps=30, duration_s=12)
    assert np.all(np.isfinite(fp.cx)) and np.all(fp.span_y > 0)
    r = jerk_report(fp)
    assert r["max_accel_zoom"] < 0.12


def test_dateline_route_is_continuous(fix):
    from timelinerx.journeys.journey import JourneyConfig, build_journey
    from timelinerx.timeline.parser import load_timeline
    j = build_journey(load_timeline(fix / "dateline.json", compute_sha=False), JourneyConfig())
    # unwrapped projection: no jump of half the world between consecutive points
    from timelinerx.core.geo import WORLD_SPAN
    assert max(abs(b - a) for a, b in zip(j.xs, j.xs[1:])) < WORLD_SPAN / 3
    fp = plan_frames(j, CameraConfig(mode="balanced"), width=1280, height=720, fps=30, duration_s=10)
    assert np.max(np.abs(np.diff(fp.cx) / fp.span_y[1:])) < 0.2


def test_outlier_filter_removes_teleport(fix):
    from timelinerx.journeys.journey import JourneyConfig, build_journey
    from timelinerx.timeline.parser import load_timeline
    tl = load_timeline(fix / "outliers.json", compute_sha=False)
    on = build_journey(tl, JourneyConfig())
    off = build_journey(tl, JourneyConfig(outlier_filter="off"))
    assert on.outliers_removed == 1 and off.outliers_removed == 0
    assert on.total_km < 10 < off.total_km
