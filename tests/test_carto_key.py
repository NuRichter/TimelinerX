"""CARTO API key handling: no silent watermarked renders, cache separation, key resolution."""

import pytest

from conftest import make_project


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch):
    from timelinerx.maps import keys
    for v in ("TIMELINERX_CARTO_KEY", "CARTO_BASEMAP_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(keys, "_build_key", lambda: "")
    keys.set_user_key("")
    yield
    keys.set_user_key("")


def test_key_resolution_order(monkeypatch):
    from timelinerx.maps import keys
    assert keys.resolve_key() == "" and keys.key_source() == "none"
    monkeypatch.setattr(keys, "_build_key", lambda: "build_key_1")
    assert keys.resolve_key() == "build_key_1" and keys.key_source() == "build"
    monkeypatch.setenv("CARTO_BASEMAP_API_KEY", "env_key_2")
    assert keys.resolve_key() == "env_key_2" and keys.key_source() == "environment"
    keys.set_user_key("  user_key_3 ")
    assert keys.resolve_key() == "user_key_3" and keys.key_source() == "settings"


def test_keyed_url_and_cache_namespace():
    from timelinerx.maps.tiles import CartoProvider, TileCache, cache_namespace, missing_key
    no = CartoProvider("voyager", api_key="")
    yes = CartoProvider("voyager", api_key="abc_123")
    other = CartoProvider("voyager", api_key="zzz_999")
    assert missing_key(no) and not missing_key(yes)
    assert yes.url(5, 25, 16) == "https://basemaps.cartocdn.com/rastertiles/voyager/5/25/16.png?key=abc_123"
    assert "key=" not in no.url(1, 0, 0)
    ns = {cache_namespace(p) for p in (no, yes, other)}
    assert len(ns) == 3, "watermarked (unkeyed) tiles must never share a cache with keyed tiles"
    assert "abc_123" not in cache_namespace(yes)          # the key itself never becomes a path
    assert TileCache(yes, __import__("pathlib").Path("/tmp")).root.name == cache_namespace(yes)


def test_render_refuses_carto_without_key(tmp_path, fix):
    from timelinerx.core.errors import TileProviderError
    from timelinerx.pipeline.render_job import RenderJob
    proj = make_project(fix / "small.json", duration_s=5)
    proj.visual.map_provider = "carto-voyager"
    with pytest.raises(TileProviderError) as ei:
        RenderJob(proj, tmp_path / "x.mp4", add_to_library=False).run()
    assert "API KEY REQUIRED" in str(ei.value) and "Settings" in (ei.value.hint or "")
    assert not (tmp_path / "x.mp4").exists()


def test_preview_says_it_uses_plain_background_without_key(fix):
    from timelinerx.pipeline.preview import PreviewSession
    proj = make_project(fix / "small.json", duration_s=5)
    proj.visual.map_provider = "carto-light"
    s = PreviewSession(proj, "480p")
    assert s.notice == "carto_key_missing"
    assert s.cache is None                     # no tile requests at all
    s.render_frame(0)


def test_custom_mirror_needs_no_key():
    from timelinerx.maps.tiles import CartoProvider, missing_key
    assert not missing_key(CartoProvider("light", api_key="", base_url="http://127.0.0.1:1/{z}/{x}/{y}.png"))
