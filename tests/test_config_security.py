import json
import re
import subprocess
from pathlib import Path

import numpy as np
import pytest

from timelinerx.core.errors import (FallbackRequiresConfirmationError, PluginError, ProjectError,
                                             UsageError)
from timelinerx.core.fallback import FallbackPolicy
from timelinerx.encoding import ffmpeg as ff
from timelinerx.projects.project import (ASPECTS, FPS_CHOICES, RESOLUTIONS, Project, resolve_dimensions)
from timelinerx.rendering.styles import load_themes, theme_from_dict, validate_theme_dict

REQUIRED_THEMES = ["light", "dark", "neon_dark_blue", "neon_dark_red", "neon_dark_yellow", "neon_dark_green",
                   "neon_dark_purple", "neon_cyan", "monochrome", "high_contrast"]


# --------------------------------------------------------------- resolutions
@pytest.mark.parametrize("res", RESOLUTIONS)
@pytest.mark.parametrize("aspect", list(ASPECTS))
def test_every_resolution_and_aspect(res, aspect):
    w, h = resolve_dimensions(res, aspect)
    assert w % 2 == 0 and h % 2 == 0
    aw, ah = ASPECTS[aspect]
    assert abs(w / h - aw / ah) < 0.01
    if res.endswith("p"):
        assert min(w, h) == int(res[:-1])


def test_named_sizes():
    assert resolve_dimensions("1080p", "16:9") == (1920, 1080)
    assert resolve_dimensions("2160p", "16:9") == (3840, 2160)
    assert resolve_dimensions("4K", "16:9") == (4096, 2304)
    assert resolve_dimensions("8K", "16:9") == (7680, 4320)
    assert resolve_dimensions("1080p", "9:16") == (1080, 1920)


def test_custom_dimensions_validated():
    assert resolve_dimensions("1080p", "custom", (1234, 700)) == (1234, 700)
    with pytest.raises(UsageError):
        resolve_dimensions("1080p", "custom", (1233, 700))
    with pytest.raises(UsageError):
        resolve_dimensions("1080p", "custom", (64, 64))
    with pytest.raises(UsageError):
        resolve_dimensions("999p", "16:9")


@pytest.mark.parametrize("fps", FPS_CHOICES)
def test_fps_choices_valid(fps):
    p = Project()
    p.video.fps = fps
    assert not [e for e in p.validate() if "FPS" in e]


def test_project_validation_and_roundtrip(tmp_path):
    p = Project()
    p.video.fps = 25
    p.video.hdr = True
    errs = p.validate()
    assert any("FPS" in e for e in errs) and any("HDR" in e for e in errs)
    q = Project(name="Roundtrip")
    q.camera.keyframes = [{"time_s": 1, "lat": 1, "lon": 2, "span_km": 3}]
    path = q.save(tmp_path / "a.nrproj")
    r = Project.load(path)
    assert r.to_dict() == q.to_dict()
    d = q.to_dict()
    d["video"]["resolutoin"] = "1080p"
    with pytest.raises(ProjectError):
        Project.from_dict(d)
    with pytest.raises(ProjectError):
        Project.from_dict({"format": "other"})


# -------------------------------------------------------------------- themes
def test_required_themes_present_and_grade():
    ts = load_themes(reload=True)
    for t in REQUIRED_THEMES:
        assert t in ts
    img = (np.random.default_rng(0).random((32, 32, 3)) * 255).astype(np.uint8)
    for t in ts.values():
        out = t.grade(img)
        assert out.shape == img.shape and out.dtype == np.uint8
    assert any(t.experimental for t in ts.values())


def test_themes_are_distinct():
    ts = load_themes()
    img = (np.linspace(0, 255, 64 * 64 * 3).reshape(64, 64, 3)).astype(np.uint8)
    outs = {k: ts[k].grade(img).tobytes() for k in REQUIRED_THEMES}
    assert len(set(outs.values())) == len(REQUIRED_THEMES)


def test_theme_validation():
    assert validate_theme_dict({"id": "x"})
    with pytest.raises(PluginError):
        theme_from_dict({"id": "x", "name": "X", "base": "light", "grading": [{"node": "rm -rf"}], "palette": {}})


def test_plugin_theme_loaded_from_directory(tmp_path):
    from timelinerx.plugins.loader import load_plugins
    (tmp_path / "themes").mkdir()
    base = json.loads((Path(__file__).parents[1] / "assets/themes/light.json").read_text(encoding="utf-8"))
    base.update(id="my_sepia", name="My Sepia")
    (tmp_path / "themes" / "sepia.json").write_text(json.dumps(base), encoding="utf-8")
    (tmp_path / "themes" / "bad.json").write_text("{not json", encoding="utf-8")
    rep = load_plugins(tmp_path)
    assert "my_sepia" in rep.themes
    assert any("bad.json" in e for e in rep.errors)
    load_themes(reload=True)


def test_provider_plugin_contract(tmp_path):
    from timelinerx.plugins.loader import load_plugins
    pd = tmp_path / "providers"
    pd.mkdir()
    (pd / "good.py").write_text(
        "from timelinerx.maps.tiles import RateLimitPolicy, TileResult\n"
        "PROVIDER_ID='my-tiles'\n"
        "class P:\n id='my-tiles'; name='Mine'; max_zoom=18; tile_size=256; base_flavor='light'\n"
        " def fetch_tile(self,z,x,y): return TileResult(False, error='none')\n"
        " def attribution(self): return '© Me'\n"
        " def rate_limit(self): return RateLimitPolicy(1, 1)\n"
        "def create_provider(): return P()\n", encoding="utf-8")
    (pd / "noattr.py").write_text(
        "from timelinerx.maps.tiles import RateLimitPolicy\nPROVIDER_ID='x2'\n"
        "class P:\n id='x2'; name='x'; max_zoom=18; tile_size=256; base_flavor='light'\n"
        " def fetch_tile(self,z,x,y): pass\n def attribution(self): return ''\n"
        " def rate_limit(self): return RateLimitPolicy()\n"
        "def create_provider(): return P()\n", encoding="utf-8")
    (pd / "crash.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    off = load_plugins(tmp_path, load_python=False)
    assert not off.providers
    rep = load_plugins(tmp_path, load_python=True)
    assert list(rep.providers) == ["my-tiles"]
    assert any("noattr" in e and "attribution" in e for e in rep.errors)
    assert any("crash" in e for e in rep.errors)
    from timelinerx.maps.tiles import make_provider
    assert make_provider("my-tiles").attribution() == "© Me"
    load_themes(reload=True)


# ------------------------------------------------------------------ encoders
def _info(hw_ok):
    i = ff.FfmpegInfo("ffmpeg", "ffprobe", "test", encoders={"libx264", "libx265", "h264_nvenc", "h264_qsv",
                                                              "hevc_nvenc"})
    i.hw_verified = {"h264_nvenc": hw_ok, "h264_qsv": False, "hevc_nvenc": hw_ok}
    i.hw_errors = {"h264_nvenc": "no driver", "h264_qsv": "no device"}
    return i


def test_encoder_auto_prefers_verified_hardware():
    pol = FallbackPolicy()
    c = ff.select_encoder(_info(True), "h264", "auto", 1920, 1080, pol)
    assert c.name == "h264_nvenc" and not pol.applied


def test_encoder_auto_fallback_is_reported_not_silent():
    seen = []
    pol = FallbackPolicy(notify=seen.append)
    c = ff.select_encoder(_info(False), "h264", "auto", 1920, 1080, pol)
    assert c.name == "libx264"
    assert seen and "no driver" in seen[0].reason and not seen[0].requires_confirmation


def test_explicit_encoder_fallback_needs_consent():
    with pytest.raises(FallbackRequiresConfirmationError):
        ff.select_encoder(_info(False), "h264", "nvenc", 1920, 1080, FallbackPolicy())
    c = ff.select_encoder(_info(False), "h264", "nvenc", 1920, 1080, FallbackPolicy(confirmer=lambda d: True))
    assert c.name == "libx264"


def test_encoder_dimension_limits():
    pol = FallbackPolicy()
    c = ff.select_encoder(_info(True), "h264", "auto", 7680, 4320, pol)
    assert c.name == "libx264" and "4096" in pol.applied[0].reason
    c2 = ff.select_encoder(_info(True), "hevc", "auto", 7680, 4320, FallbackPolicy())
    assert c2.name == "hevc_nvenc"


def test_subprocess_requires_argument_list():
    with pytest.raises(TypeError):
        ff.popen("ffmpeg -version")
    with pytest.raises(TypeError):
        ff.popen(["ffmpeg", 3])


def test_ffmpeg_missing_is_explicit(monkeypatch):
    from timelinerx.core.errors import FfmpegUnavailableError
    monkeypatch.setattr(ff, "locate", lambda *a, **k: None)
    with pytest.raises(FfmpegUnavailableError) as e:
        ff.probe(force=True)
    assert e.value.hint


# ------------------------------------------------------------------- privacy
def test_log_sanitizer():
    from timelinerx.utils.logging_setup import sanitize
    s = sanitize('point "-7.2575123°, 112.7521456°" and latitudeE7: -72575123 and 3.5, 2.1')
    assert "7.2575123" not in s and "72575123" not in s and "<coord>" in s
    assert "3.5, 2.1" in s


def test_log_filter_applies(tmp_path):
    import logging
    from timelinerx.utils.logging_setup import log, setup_logging
    p = setup_logging(tmp_path)
    log.info("at %s", "-6.2088123, 106.8456123")
    for h in log.handlers:
        h.flush()
    assert "106.8456123" not in p.read_text(encoding="utf-8")


def test_safe_filename():
    from timelinerx.utils.paths import safe_filename
    assert safe_filename('../..\\evil:<name>?') == "_.._evil__name__"   # no leading dot / separators
    assert safe_filename("CON") == "_CON"
    assert safe_filename("   ") == "untitled"


# --------------------------------------------------------------------- i18n
def test_all_ui_keys_translated():
    from timelinerx.i18n import all_keys
    src = Path(__file__).parents[1] / "src" / "timelinerx"
    used = set()
    for f in src.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        used |= set(re.findall(r'tr\(\s*"([a-z0-9_.]+)"', text))
        used |= set(re.findall(r"tr\(\s*'([a-z0-9_.]+)'", text))
    used = {k for k in used if "." in k and not k.endswith(".")}
    en = all_keys("en")
    assert not (used - en), f"missing English: {sorted(used - en)}"
    from timelinerx.i18n import LANGUAGES, _load
    ph = re.compile(r"\{[a-z_0-9]*\}")
    for lang in LANGUAGES:
        keys = all_keys(lang)
        extra = {k for k in keys - en if not k.startswith("month.gen.")}
        assert keys >= en and not extra, (lang, sorted(en - keys)[:10], sorted(extra)[:10])
        table = _load(lang)
        for k, v in _load("en").items():
            if k.startswith("video.date."):
                assert set(ph.findall(table[k])) <= {"{day}", "{month}", "{year}", "{m}"}, (lang, k)
            else:
                assert sorted(ph.findall(v)) == sorted(ph.findall(table[k])), (lang, k)


def test_localised_numbers_dates_and_rtl():
    from datetime import datetime
    from timelinerx.i18n import format_date, format_distance, is_rtl, month_name
    assert format_distance(24209.0, "km", "de") == "24.209,0\u00a0km"
    assert format_distance(1609.344, "mi", "en") == "1,000.0\u00a0mi"
    assert format_distance(1000, "km", "fr").startswith("1\u202f000,0")
    assert format_date(datetime(2026, 6, 5), "ru") == "5 июня 2026 г."
    assert format_date(datetime(2026, 6, 5), "ja") == "2026年6月5日"
    assert month_name(6, "es") == "junio"
    assert is_rtl("ar") and not is_rtl("en")


def test_eta_estimator_is_honest():
    from timelinerx.pipeline.progress import EtaEstimator
    e = EtaEstimator(total_frames=1000, min_samples=20)
    for _ in range(10):
        e.frame_done(0.1)
    assert e.eta_seconds() is None and e.to_dict()["eta_state"] == "estimating"
    for _ in range(40):
        e.frame_done(0.1)
    assert e.eta_seconds() == pytest.approx(950 * 0.1 * (1 + e.tail_ratio), rel=0.01)
    e.skip(500)
    assert e.done == 550
