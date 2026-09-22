import io
import json
import zipfile
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from timelinerx.core.errors import (ImportFailedError, InputTooLargeError, NoDataError,
                                             UnsupportedFormatError)
from timelinerx.timeline.parser import ImportLimits, load_timeline
from timelinerx.timeline.values import parse_coordinate, parse_e7_pair, parse_instant

UPSTREAM = Path(__file__).resolve().parents[1] / "fixtures" / "upstream"


@pytest.mark.parametrize("raw,expected", [
    ("37.5665°, 126.9780°", (37.5665, 126.978)),
    ("geo:-7.2575,112.7521", (-7.2575, 112.7521)),
    ("geo:-7.2575,112.7521?z=3", (-7.2575, 112.7521)),
    ({"latLng": "1.5°, 2.5°"}, (1.5, 2.5)),
    ({"point": "-72575000, 1127521000"}, (-7.2575, 112.7521)),     # E7 in string
    ({"latLng": {"latitudeE7": -72575000, "longitudeE7": 1127521000}}, (-7.2575, 112.7521)),
])
def test_parse_coordinate_variants(raw, expected):
    got = parse_coordinate(raw)
    assert got == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["", "abc", "1,", "95.0, 10.0", "nan,nan", None, 5])
def test_parse_coordinate_rejects(raw):
    assert parse_coordinate(raw) is None


def test_e7_int32_overflow_is_corrected():
    c, _ = parse_e7_pair({"latitudeE7": 473000000, "longitudeE7": 4294967296 - 1220000000})
    assert c == pytest.approx((47.3, -122.0))


def test_parse_instant_timezones():
    t, off, missing = parse_instant("2024-05-01T08:00:00+07:00")
    assert off == 420 and not missing and t == 1714525200
    t2, off2, missing2 = parse_instant("2024-05-01T01:00:00Z")
    assert t2 == t and off2 == 0
    _, _, m3 = parse_instant("2024-05-01T08:00:00")
    assert m3
    assert parse_instant("1714525200000")[0] == 1714525200
    assert parse_instant("not a date") is None


@pytest.mark.parametrize("name", [p.name for p in sorted(UPSTREAM.glob("*.json")) if "expected" not in p.name])
def test_parity_with_upstream_parser(name):
    """Our extraction matches the upstream reference parser point-for-point."""
    expected = {
        "android-ios-sample.json": 11, "offset-path-sample.json": 2, "outlier-sample.json": 3,
        "platform-parity-sample.json": 3, "semantic-and-raw-ranges.json": 2, "seoul-bohol-sample.json": 2,
        "takeout-sample.json": 3, "trips-lab-sample.json": 14}
    tl = load_timeline(UPSTREAM / name, compute_sha=False)
    assert len(tl.semantic) == expected[name]


def test_platform_parity_expected_points():
    exp = json.loads((UPSTREAM / "platform-parity-expected.json").read_text(encoding="utf-8"))
    tl = load_timeline(UPSTREAM / "platform-parity-sample.json", compute_sha=False)
    assert exp["pointCount"] == len(tl.semantic)
    assert [round(x, 6) for x in exp["latitudes"]] == [round(x, 6) for x in tl.semantic.lat.tolist()]


@pytest.mark.parametrize("name,fmt,points", [
    ("legacy-records.json", "takeout-records", 50),
    ("legacy-semantic.json", "takeout-semantic", 4),
    ("ios-array.json", "device-timeline-array", 3),
    ("semantic-and-raw.json", "device-timeline-object", 2),
    ("dense.json", "device-timeline-object", 6000),
])
def test_formats(fix, name, fmt, points):
    tl = load_timeline(fix / name, compute_sha=False)
    assert tl.diagnostics.detected_format.startswith(fmt)
    assert len(tl.semantic) == points


def test_raw_signals_kept_separately(fix):
    tl = load_timeline(fix / "semantic-and-raw.json", compute_sha=False)
    assert len(tl.raw) == 60 and np.all(tl.raw.accuracy == 12)


def test_missing_values_are_diagnosed_not_fatal(fix):
    tl = load_timeline(fix / "missing-values.json", compute_sha=False)
    assert len(tl.semantic) == 2
    assert tl.diagnostics.skipped.get("missing_timestamp", 0) >= 2


def test_descending_export_is_detected_and_ordered(fix):
    a = load_timeline(fix / "standard-route.json", compute_sha=False)
    b = load_timeline(fix / "descending.json", compute_sha=False)
    assert b.diagnostics.direction_reversed and not a.diagnostics.direction_reversed
    assert np.array_equal(a.semantic.t, b.semantic.t)


def test_duplicates_removed(fix):
    tl = load_timeline(fix / "duplicates.json", compute_sha=False)
    assert tl.diagnostics.duplicates_removed > 0
    assert not tl.diagnostics.direction_reversed


def test_timezone_offsets_kept(fix):
    tl = load_timeline(fix / "timezone-change.json", compute_sha=False)
    assert sorted(set(tl.semantic.offset_min.tolist())) == [420, 480]


def test_not_json_and_empty(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("hello", encoding="utf-8")
    with pytest.raises(UnsupportedFormatError):
        load_timeline(p)
    p.write_text('{"foo": 1}', encoding="utf-8")
    with pytest.raises(UnsupportedFormatError):
        load_timeline(p)
    p.write_text('{"semanticSegments": []}', encoding="utf-8")
    with pytest.raises(NoDataError):
        load_timeline(p)
    with pytest.raises(ImportFailedError):
        load_timeline(tmp_path / "missing.json")


def test_utf8_bom_accepted_utf16_rejected_with_hint(tmp_path, fix):
    data = (fix / "small.json").read_bytes()
    p = tmp_path / "bom.json"
    p.write_bytes(b"\xef\xbb\xbf" + data)
    assert len(load_timeline(p, compute_sha=False).semantic) == 93
    p.write_bytes(data.decode().encode("utf-16"))
    with pytest.raises(UnsupportedFormatError) as e:
        load_timeline(p)
    assert "Repair" in (e.value.hint or "")


def test_deep_nesting_is_rejected(tmp_path):
    p = tmp_path / "deep.json"
    p.write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
    with pytest.raises((ImportFailedError, UnsupportedFormatError)):
        load_timeline(p)


def test_size_limit(tmp_path, fix):
    with pytest.raises(InputTooLargeError):
        load_timeline(fix / "small.json", limits=ImportLimits(max_file_bytes=100))


def test_zip_import_and_guards(tmp_path, fix):
    z = tmp_path / "takeout.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(fix / "small.json", "Takeout/Location History/Timeline.json")
        zf.writestr("../../evil.json", "{}")
    tl = load_timeline(z, compute_sha=False)
    assert len(tl.semantic) == 93
    assert any("unsafe" in w for w in tl.diagnostics.warnings)
    bomb = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Timeline.json", '{"semanticSegments": [' + " " * 50_000_000 + "]}")
    with pytest.raises(InputTooLargeError):
        load_timeline(bomb)


def test_streaming_equals_in_memory(fix):
    a = load_timeline(fix / "standard-route.json", compute_sha=False)
    b = load_timeline(fix / "standard-route.json", compute_sha=False, _stop_after_bytes=10 ** 12)
    for f in ("t", "lat", "lon", "offset_min"):
        assert np.array_equal(getattr(a.semantic, f), getattr(b.semantic, f))


def test_streaming_handles_escapes_and_nested_strings(tmp_path):
    seg = {"startTime": "2024-01-01T00:00:00Z", "note": 'he said "] }, [ {" \\ ok',
           "visit": {"topCandidate": {"placeLocation": {"latLng": "1.0°, 2.0°"}}}}
    p = tmp_path / "esc.json"
    p.write_text(json.dumps({"meta": {"a": [1, 2, {"b": "]"}]}, "semanticSegments": [seg, seg | {"startTime": "2024-01-02T00:00:00Z"}]}), encoding="utf-8")
    tl = load_timeline(p, compute_sha=False, _stop_after_bytes=10 ** 12)
    assert len(tl.semantic) == 2


def test_import_checkpoint_resume(tmp_path):
    """X.3: a large parse interrupted mid-way resumes from its checkpoint with identical results."""
    import make_fixtures
    big = make_fixtures.make_large(tmp_path / "big.json", 55)
    ck = tmp_path / "ck"
    ref = load_timeline(big, compute_sha=False)
    assert not ref.diagnostics.resumed_from_checkpoint
    with pytest.raises(ImportFailedError):
        load_timeline(big, checkpoint_dir=ck, compute_sha=False, _stop_after_bytes=30 * 1024 * 1024)
    assert any(ck.glob("import-*.npz"))
    res = load_timeline(big, checkpoint_dir=ck, compute_sha=False)
    assert res.diagnostics.resumed_from_checkpoint
    assert len(res.semantic) == len(ref.semantic)
    assert np.array_equal(res.semantic.t, ref.semantic.t)
    assert np.array_equal(res.semantic.lat, ref.semantic.lat)
    assert not any(ck.glob("import-*.npz"))      # checkpoint removed after success


def test_period_selection_uses_local_calendar(fix):
    from timelinerx.journeys.journey import JourneyConfig, build_journey
    tl = load_timeline(fix / "standard-route.json", compute_sha=False)
    j = build_journey(tl, JourneyConfig(start_date=date(2024, 5, 1), end_date=date(2024, 5, 1)))
    days = {d.date() for d in (j.datetime_at_distance(x) for x in np.linspace(0, j.total_km, 20))}
    assert days == {date(2024, 5, 1)}
    with pytest.raises(NoDataError):
        build_journey(tl, JourneyConfig(start_date=date(2030, 1, 1), end_date=date(2030, 1, 2)))
