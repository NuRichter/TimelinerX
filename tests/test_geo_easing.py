import math

import numpy as np
import pytest

from timelinerx.core import easing as E
from timelinerx.core.geo import (cumulative_km, haversine_km, interpolate_great_circle, latlon_to_meters,
                                          meters_to_latlon, position_at_distance, unwrap_longitudes)


def test_projection_roundtrip():
    for lat, lon in [(0, 0), (-7.2575, 112.7521), (51.5, -0.12), (84.9, 179.9), (-84.9, -179.9)]:
        x, y = latlon_to_meters(lat, lon)
        la, lo = meters_to_latlon(x, y)
        assert la == pytest.approx(lat, abs=1e-9) and lo == pytest.approx(lon, abs=1e-9)


def test_haversine_known_distance():
    # Jakarta – Surabaya ≈ 663 km great-circle
    assert haversine_km(-6.2088, 106.8456, -7.2575, 112.7521) == pytest.approx(663, abs=5)
    assert haversine_km(1, 1, 1, 1) == 0


def test_great_circle_endpoints_and_midpoint():
    a, b = (-6.2, 106.8), (35.7, 139.7)
    assert interpolate_great_circle(*a, *b, 0) == a
    assert interpolate_great_circle(*a, *b, 1) == b
    mid = interpolate_great_circle(*a, *b, 0.5)
    d1, d2 = haversine_km(*a, *mid), haversine_km(*mid, *b)
    assert d1 == pytest.approx(d2, rel=1e-6)


def test_position_at_distance_monotone():
    lats, lons = [0, 0, 0], [0, 1, 2]
    cum = cumulative_km(lats, lons)
    ps = [position_at_distance(cum, lats, lons, d)[1] for d in np.linspace(0, cum[-1], 50)]
    assert all(b >= a - 1e-12 for a, b in zip(ps, ps[1:]))


def test_unwrap_longitudes_across_dateline():
    assert unwrap_longitudes([170, 179, -179, -170]) == [170, 179, 181, 190]
    assert unwrap_longitudes([-170, 179]) == [-170, -181]


@pytest.mark.parametrize("name", list(E.EASINGS))
def test_easing_endpoints_and_c1(name):
    f = E.EASINGS[name]
    assert f(0) == pytest.approx(0, abs=1e-9)
    assert f(1) == pytest.approx(1, abs=1e-9)
    if name == "linear":
        return
    # first derivative is continuous: no jumps between adjacent numeric derivatives
    h = 1e-4
    ts = np.linspace(h, 1 - h, 2000)
    d = np.array([(f(t + h) - f(t - h)) / (2 * h) for t in ts])
    assert np.max(np.abs(np.diff(d))) < 0.02


def test_ease_out_back_overshoot_is_damped():
    peak = max(E.ease_out_back_damped(t) for t in np.linspace(0, 1, 1001))
    assert 1.0 < peak < 1.05


def test_ramped_progress_properties():
    from timelinerx.camera.planner import ramped_progress
    us = np.linspace(0, 1, 2001)
    p = np.array([ramped_progress(u, 0.08) for u in us])
    assert p[0] == 0 and p[-1] == pytest.approx(1, abs=1e-9)
    assert np.all(np.diff(p) >= -1e-12)
    v = np.diff(p)
    assert v[0] < v[1000] * 0.05                 # starts from rest
    assert np.max(np.abs(np.diff(v))) < 1e-5     # velocity continuous


def test_zoom_pan_interpolator_endpoints_and_scale():
    a, b = (0.0, 0.0, 1000.0), (50_000.0, 20_000.0, 10.0)
    f = E.zoom_pan_interpolator(a, b)
    assert f(0) == pytest.approx(a, rel=1e-9)
    assert f(1)[0] == pytest.approx(b[0], rel=1e-6)
    assert f(1)[2] == pytest.approx(b[2], rel=1e-6)
    spans = [f(t)[2] for t in np.linspace(0, 1, 50)]
    assert all(s > 0 for s in spans)


def test_gaussian_smooth_zero_phase():
    x = np.zeros(201)
    x[100] = 1.0
    y = np.array(E.gaussian_smooth(x.tolist(), 5))
    assert int(np.argmax(y)) == 100      # no lag
    assert y.sum() == pytest.approx(1.0, abs=1e-6)
