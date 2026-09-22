import hashlib
import json
import shutil

import pytest

from timelinerx.repair.engine import Confidence, analyze, apply_plan
from timelinerx.timeline.parser import load_timeline


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_truncated_file_is_recovered_without_touching_source(tmp_path, fix):
    src = tmp_path / "Timeline.json"
    shutil.copy(fix / "broken-truncated.json", src)
    before = _sha(src)
    plan = analyze(src)
    assert not plan.structurally_valid
    salv = [a for a in plan.actions if a.id == "struct-salvage"]
    assert salv and salv[0].confidence == Confidence.MEDIUM
    res = apply_plan(plan)
    assert _sha(src) == before                                   # original untouched
    assert res.fixed_path.name == "Timeline.fixed.json"
    for p in (res.fixed_path, res.report_json, res.report_html, res.log_path):
        assert p.is_file() and p.stat().st_size > 0
    assert res.validation["ok"] and res.validation["semantic_points"] > 100


def test_bom_and_trailing_commas(tmp_path, fix):
    src = tmp_path / "t.json"
    shutil.copy(fix / "broken-bom-trailing-comma.json", src)
    plan = analyze(src)
    ids = {a.id: a for a in plan.actions}
    assert ids["enc-bom"].confidence == Confidence.HIGH
    assert ids["struct-trailing-commas"].confidence == Confidence.MEDIUM
    assert apply_plan(plan).validation["ok"]


def test_confidence_taxonomy_and_defaults(tmp_path):
    t = "2024-03-01T0{h}:00:00+00:00"
    segs = [{"startTime": t.format(h=h), "visit": {"topCandidate": {"placeLocation": {"latLng": f"-7.2{h}°, 112.7{h}°"}}}}
            for h in range(6)]
    segs.insert(3, {"startTime": "2024-03-01T02:30:00+00:00",
                    "visit": {"topCandidate": {"placeLocation": {"latLng": "48.85°, 2.35°"}}}})    # teleport
    segs.append(segs[0])                                                                          # exact dup
    segs.append({"startTime": "2024-03-01T07:00:00+00:00",
                 "visit": {"topCandidate": {"placeLocation": {"latLng": "112.8°, -7.3°"}}}})       # swapped
    segs.append({"startTime": "2024-03-01T08:00:00+00:00", "endTime": "2024-03-01T07:00:00+00:00",
                 "visit": {"topCandidate": {"placeLocation": {"latLng": "-7.3°, 112.8°"}}}})       # end<start
    src = tmp_path / "t.json"
    src.write_text(json.dumps({"semanticSegments": segs}), encoding="utf-8")
    plan = analyze(src)
    by = {(a.pass_name, a.confidence) for a in plan.actions}
    assert ("duplicate_cleanup", Confidence.HIGH) in by
    assert ("outlier_analysis", Confidence.MEDIUM) in by
    assert ("coordinate_repair", Confidence.LOW) in by
    assert ("timestamp_repair", Confidence.LOW) in by
    for a in plan.actions:
        assert a.default_accept == (a.confidence != Confidence.LOW)

    res = apply_plan(plan)                             # LOW not applied by default
    assert all(not a.applied for a in plan.actions if a.confidence == Confidence.LOW)
    fixed = json.loads(res.fixed_path.read_text(encoding="utf-8"))
    lat_lngs = [s["visit"]["topCandidate"]["placeLocation"]["latLng"] for s in fixed["semanticSegments"]]
    assert "48.85°, 2.35°" not in lat_lngs            # MEDIUM excursion removed
    assert "112.8°, -7.3°" in lat_lngs                # LOW swap not applied

    res2 = apply_plan(plan, accept_low=True)
    fixed2 = json.loads(res2.fixed_path.read_text(encoding="utf-8"))
    lat2 = [s["visit"]["topCandidate"]["placeLocation"]["latLng"] for s in fixed2["semanticSegments"]]
    assert "-7.3000000°, 112.8000000°" in lat2
    assert res2.validation["ok"]


def test_reject_specific_action(tmp_path, fix):
    src = tmp_path / "d.json"
    shutil.copy(fix / "duplicates.json", src)
    plan = analyze(src)
    dup = [a for a in plan.actions if a.pass_name == "duplicate_cleanup"][0]
    res = apply_plan(plan, reject_ids={dup.id})
    assert dup in res.rejected
    n_before = len(json.loads(src.read_text(encoding="utf-8"))["semanticSegments"])
    assert len(json.loads(res.fixed_path.read_text(encoding="utf-8"))["semanticSegments"]) == n_before


def test_unrecoverable(tmp_path):
    src = tmp_path / "x.json"
    src.write_text("this is not json at all", encoding="utf-8")
    plan = analyze(src)
    assert plan.document is None
    with pytest.raises(Exception):
        apply_plan(plan)


def test_clean_file_needs_nothing(tmp_path, fix):
    src = tmp_path / "s.json"
    shutil.copy(fix / "sparse.json", src)
    plan = analyze(src)
    assert plan.actions == []
    assert apply_plan(plan).validation["ok"]


def test_report_html_escapes_content(tmp_path):
    src = tmp_path / "x.json"
    src.write_text('{"semanticSegments": [{"startTime": "<script>alert(1)</script>", '
                   '"visit": {"topCandidate": {"placeLocation": {"latLng": "1°, 2°"}}}},'
                   '{"startTime": "2024-01-01T00:00:00Z", "visit": {"topCandidate": {"placeLocation": {"latLng": "1°, 2°"}}}}]}', encoding="utf-8")
    res = apply_plan(analyze(src))
    html = res.report_html.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html
