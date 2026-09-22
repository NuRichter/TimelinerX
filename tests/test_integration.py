"""Integration tests: full pipeline, FFmpeg, resume after SIGKILL, cancellation, tiles, audio, HDR, CLI."""

import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import wave
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from conftest import ROOT, make_project, needs_ffmpeg
from timelinerx.core.errors import (FallbackRequiresConfirmationError, RenderCancelledError, UsageError)
from timelinerx.core.fallback import FallbackPolicy
from timelinerx.encoding import ffmpeg as ff
from timelinerx.pipeline.render_job import RenderJob
from timelinerx.storage.library import Library
from timelinerx.utils.cancel import CancelToken

pytestmark = needs_ffmpeg
ENV = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8")


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                          "-count_packets", str(path)], capture_output=True, check=True)
    return json.loads(out.stdout)


def ffmpeg_procs() -> int:
    """Live ffmpeg processes (portable). Zombies are already dead; they only await reaping by init."""
    import psutil
    n = 0
    for p in psutil.process_iter(["name", "status"]):
        try:
            if (p.info["name"] or "").lower() in ("ffmpeg", "ffmpeg.exe") and \
                    p.info["status"] != psutil.STATUS_ZOMBIE:
                n += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return n


def hard_kill(proc) -> None:
    """Abrupt termination with no cleanup: SIGKILL on POSIX, TerminateProcess on Windows."""
    proc.kill()


# --------------------------------------------------------------------- basic
def test_full_render_verified_and_in_library(tmp_path, fix):
    proj = make_project(fix / "standard-route.json", duration_s=6)
    lib = Library(tmp_path / "lib.sqlite3")
    events = []
    out = tmp_path / "out" / "video.mp4"
    res = RenderJob(proj, out, library=lib, listener=events.append, segment_seconds=2).run()
    assert out.is_file()
    info = probe(out)
    v = info["streams"][0]
    assert (v["width"], v["height"]) == (852, 480)
    assert v["r_frame_rate"] == "30/1" and int(v["nb_read_packets"]) == 180
    assert v.get("color_space") == "bt709"
    entries = lib.list()
    assert len(entries) == 1 and entries[0].fps == 30 and Path(entries[0].thumbnail).is_file()
    meta = json.loads(Path(str(out) + ".nrmeta.json").read_text(encoding="utf-8"))
    assert meta["render_engine_version"] and meta["fallbacks_applied"] is not None
    kinds = {e["event"] for e in events}
    assert {"job_started", "node_done", "segment_committed", "job_done"} <= kinds
    assert not Path(res["job_dir"]).exists()                      # temp job files removed on success
    # sidecar never contains coordinates
    assert '"lat"' not in json.dumps(meta["project"]["camera"])


@pytest.mark.parametrize("fps", [24, 60])
def test_frame_rates(tmp_path, fix, fps):
    proj = make_project(fix / "small.json", duration_s=5, fps=fps)
    out = tmp_path / f"v{fps}.mp4"
    RenderJob(proj, out, add_to_library=False).run()
    v = probe(out)["streams"][0]
    assert v["r_frame_rate"] == f"{fps}/1" and int(v["nb_read_packets"]) == 5 * fps


def test_portrait_and_square(tmp_path, fix):
    for aspect, dims in (("9:16", (480, 852)), ("1:1", (480, 480))):
        proj = make_project(fix / "small.json", duration_s=5, aspect=aspect)
        out = tmp_path / f"{aspect.replace(':', 'x')}.mp4"
        RenderJob(proj, out, add_to_library=False).run()
        v = probe(out)["streams"][0]
        assert (v["width"], v["height"]) == dims


def test_existing_output_is_not_overwritten_silently(tmp_path, fix):
    out = tmp_path / "exists.mp4"
    out.write_bytes(b"precious")
    with pytest.raises(UsageError):
        RenderJob(make_project(fix / "small.json"), out, add_to_library=False).run()
    assert out.read_bytes() == b"precious"


def test_changed_timeline_is_detected(tmp_path, fix):
    tl = tmp_path / "t.json"
    tl.write_bytes((fix / "small.json").read_bytes())
    proj = make_project(tl)
    tl.write_bytes((fix / "sparse.json").read_bytes())
    from timelinerx.core.errors import ProjectError
    with pytest.raises(ProjectError):
        RenderJob(proj, tmp_path / "o.mp4", add_to_library=False).run()


# ------------------------------------------------------------ resume / cancel
def _cli_render(project_path, out, extra=()):
    return subprocess.Popen([sys.executable, "-m", "timelinerx", "render", "--project", str(project_path),
                             "--output", str(out), "--json", "--no-library", "--segment-seconds", "1", *extra],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env=ENV, text=True, encoding="utf-8")


def test_sigkill_during_encode_resumes_without_rerendering(tmp_path, fix):
    """VII.2 verification: kill the process during Encode; resume must not re-render frames."""
    # software encoder: two-pass is software-only, and a GPU on the test machine must not turn this
    # resume test into a fallback-consent test
    proj = make_project(fix / "standard-route.json", duration_s=8, resolution="720p", two_pass=True,
                        quality="cinematic", encoder="software")
    pp = proj.save(tmp_path / "p.nrproj")
    out = tmp_path / "resume.mp4"
    proc = _cli_render(pp, out)
    job_dir = None
    killed_in = None
    for line in proc.stdout:
        ev = json.loads(line)
        job_dir = ev.get("job_dir") or job_dir
        if ev.get("event") == "node_started" and ev.get("node") == "encode":
            time.sleep(0.4)                                   # let two-pass get going
            hard_kill(proc)
            killed_in = "encode"
            break
    proc.wait(timeout=60)
    assert killed_in == "encode" and job_dir
    assert not out.exists()
    time.sleep(0.5)
    assert ffmpeg_procs() == 0, "ffmpeg child outlived its killed parent"
    ck = json.loads((Path(job_dir) / "nodes" / "render_frames.json").read_text(encoding="utf-8"))
    assert ck["status"] == "done"
    r = subprocess.run([sys.executable, "-m", "timelinerx", "resume", "--job-dir", job_dir, "--json"],
                       stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=600)
    assert r.returncode == 0, r.stderr[-2000:]
    evs = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
    cached = {e["node"] for e in evs if e.get("event") == "node_cached"}
    assert "render_frames" in cached
    assert not any(e.get("event") == "segment_committed" for e in evs)
    assert not any(e.get("event") == "node_started" and e.get("node") == "render_frames" for e in evs)
    assert int(probe(out)["streams"][0]["nb_read_packets"]) == 240


def test_sigkill_during_render_frames_resumes_remaining_segments(tmp_path, fix):
    proj = make_project(fix / "standard-route.json", duration_s=8)
    pp = proj.save(tmp_path / "p.nrproj")
    out = tmp_path / "r2.mp4"
    proc = _cli_render(pp, out)
    job_dir, committed = None, 0
    for line in proc.stdout:
        ev = json.loads(line)
        job_dir = ev.get("job_dir") or job_dir
        if ev.get("event") == "segment_committed":
            committed += 1
            if committed == 3:
                hard_kill(proc)
                break
    proc.wait(timeout=60)
    r = subprocess.run([sys.executable, "-m", "timelinerx", "resume", "--job-dir", job_dir, "--json"],
                       stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=600)
    assert r.returncode == 0, r.stderr[-2000:]
    evs = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
    restored = [e for e in evs if e.get("event") == "resume"][0]["frames_restored"]
    assert restored >= 90
    new_segments = sum(1 for e in evs if e.get("event") == "segment_committed")
    assert new_segments == 8 - restored // 30
    assert int(probe(out)["streams"][0]["nb_read_packets"]) == 240


def test_cancel_cleans_up_and_leaves_no_orphans(tmp_path, fix):
    proj = make_project(fix / "standard-route.json", duration_s=20)
    cancel = CancelToken()
    started = threading.Event()

    def listener(ev):
        if ev.get("event") == "segment_committed":
            started.set()

    job = RenderJob(proj, tmp_path / "c.mp4", cancel=cancel, listener=listener, add_to_library=False,
                    segment_seconds=1)
    err = []
    t = threading.Thread(target=lambda: err.append(_run(job)))
    t.start()
    assert started.wait(120)
    cancel.cancel("test")
    t.join(60)
    assert isinstance(err[0], RenderCancelledError)
    assert ff.live_children() == 0
    time.sleep(0.3)
    assert ffmpeg_procs() == 0
    assert not list(job.job_dir.glob("segments/*.part"))      # no half-written segment left
    job.discard()
    assert not job.job_dir.exists() and not (tmp_path / "c.mp4").exists()


def _run(job):
    try:
        job.run()
    except BaseException as e:  # noqa: BLE001
        return e
    return None


# ----------------------------------------------------------------- map tiles
class _TileHandler(SimpleHTTPRequestHandler):
    missing = staticmethod(lambda z, x, y: False)
    hits = []

    def do_GET(self):
        parts = self.path.strip("/").split("/")
        z, x, y = int(parts[-3]), int(parts[-2]), int(parts[-1].split(".")[0])
        type(self).hits.append((z, x, y))
        if type(self).missing(z, x, y):
            self.send_response(404)
            self.end_headers()
            return
        rng = np.random.default_rng(z * 1_000_003 + x * 1009 + y)
        base = np.full((256, 256, 3), 200 + (x + y) % 40, np.uint8)
        yy, xx = np.mgrid[0:256, 0:256]
        base[((xx // 32 + yy // 32) % 2) == 0] -= 30
        base[:, :, 1] = np.clip(base[:, :, 1].astype(int) + rng.integers(-5, 5), 0, 255)
        buf = io.BytesIO()
        Image.fromarray(base).save(buf, "PNG")
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.end_headers()
        self.wfile.write(buf.getvalue())

    def log_message(self, *a):
        pass


@pytest.fixture
def tile_server(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    _TileHandler.missing = staticmethod(lambda z, x, y: False)
    _TileHandler.hits = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _TileHandler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    from timelinerx.maps.tiles import PLUGIN_PROVIDERS, CartoProvider

    class LocalTiles(CartoProvider):
        def __init__(self):
            super().__init__("light", base_url=f"http://127.0.0.1:{srv.server_port}/{{style}}/{{z}}/{{x}}/{{y}}.png")
            self.id = "test-local-tiles"
            self.name = "Local test tiles"

        def attribution(self):
            return "© test tiles"

    PLUGIN_PROVIDERS["test-local"] = LocalTiles
    yield _TileHandler
    PLUGIN_PROVIDERS.pop("test-local", None)
    srv.shutdown()


def test_tiles_downloaded_cached_and_graded(tmp_path, fix, tile_server):
    proj = make_project(fix / "small.json", duration_s=5)
    proj.visual.map_provider = "test-local"
    root = tmp_path / "tiles"
    RenderJob(proj, tmp_path / "t1.mp4", add_to_library=False, tile_root=root).run()
    n1 = len(tile_server.hits)
    assert n1 > 0 and len(list(root.rglob("*.tile"))) == len(set(tile_server.hits))
    assert (root.parent / "graded").exists()
    proj.visual.offline = True                              # second render: cache only, no network
    RenderJob(proj, tmp_path / "t2.mp4", add_to_library=False, tile_root=root).run()
    assert len(tile_server.hits) == n1


def test_missing_tiles_require_consent(tmp_path, fix, tile_server):
    from timelinerx.maps.compositor import tiles_for_plan
    proj = make_project(fix / "small.json", duration_s=5)
    proj.visual.map_provider = "test-local"
    tile_server.missing = staticmethod(lambda z, x, y: z >= 9 and x % 3 == 0)
    with pytest.raises(FallbackRequiresConfirmationError):
        RenderJob(proj, tmp_path / "m.mp4", add_to_library=False, tile_root=tmp_path / "tiles").run()
    seen = []
    pol = FallbackPolicy(confirmer=lambda d: seen.append(d) or True)
    res = RenderJob(proj, tmp_path / "m2.mp4", add_to_library=False, tile_root=tmp_path / "tiles",
                    policy=pol).run()
    assert seen and seen[0].subsystem == "map_tiles"
    assert any(f["subsystem"] == "map_tiles" for f in res["fallbacks"])


# --------------------------------------------------------------------- audio
def _click_track(path: Path, seconds=10.0, bpm=120, sr=22050):
    t = np.arange(int(seconds * sr)) / sr
    x = np.zeros_like(t)
    period = 60.0 / bpm
    for k in range(int(seconds / period)):
        i = int(k * period * sr)
        n = int(0.03 * sr)
        x[i:i + n] += np.sin(2 * np.pi * 1000 * t[:n]) * np.exp(-t[:n] * 80)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((x * 20000).astype(np.int16).tobytes())
    return [k * period for k in range(int(seconds / period))]


def test_onset_detection_local(tmp_path):
    from timelinerx.audio.beat import analyze_file
    truth = _click_track(tmp_path / "c.wav")
    res = analyze_file(ff.probe(verify_hw=False), tmp_path / "c.wav")
    assert len(res.onsets_s) >= len(truth) - 2
    for o in res.onsets_s:
        assert min(abs(o - t) for t in truth) < 0.05
    assert res.tempo_bpm == pytest.approx(120, rel=0.05)


def test_render_with_audio_beat_sync(tmp_path, fix):
    wav = tmp_path / "music.wav"
    _click_track(wav, seconds=12)
    proj = make_project(fix / "small.json", duration_s=8)
    proj.audio.enabled, proj.audio.path, proj.audio.beat_sync, proj.audio.ducking = True, str(wav), True, True
    res = RenderJob(proj, tmp_path / "a.mp4", add_to_library=False).run()
    info = probe(tmp_path / "a.mp4")
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert float(info["format"]["duration"]) == pytest.approx(8.0, abs=0.1)
    cam = [n for n in res["nodes"] if n["node"] == "plan_camera"][0]
    assert cam["status"] in ("done", "cached")


# ----------------------------------------------------------------------- HDR
def test_hdr_export_experimental(tmp_path, fix):
    proj = make_project(fix / "small.json", duration_s=5, codec="hevc", hdr=True)
    info_ff = ff.probe(verify_hw=False)
    out = tmp_path / "hdr.mp4"
    if ff.hdr_chain(info_ff, *proj.video.dimensions())[0] is None:   # decided by a real test encode
        with pytest.raises(FallbackRequiresConfirmationError):
            RenderJob(proj, out, add_to_library=False).run()
        return
    RenderJob(proj, out, add_to_library=False).run()
    v = probe(out)["streams"][0]
    assert v["codec_name"] == "hevc" and v.get("color_transfer") == "smpte2084"
    assert v.get("pix_fmt") == "yuv420p10le"


# --------------------------------------------------------------- comparison
def test_compare_split_and_sequential(tmp_path, fix):
    from timelinerx.pipeline.compare import compare
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    RenderJob(make_project(fix / "small.json", duration_s=5), a, add_to_library=False).run()
    RenderJob(make_project(fix / "sparse.json", duration_s=5), b, add_to_library=False).run()
    s = compare(a, b, tmp_path / "split.mp4", "split")
    v = probe(s)["streams"][0]
    assert v["width"] == 852 * 2 and v["height"] == 480
    q = compare(a, b, tmp_path / "seq.mp4", "sequential", "2023", "2024")
    assert float(probe(q)["format"]["duration"]) == pytest.approx(15.0, abs=0.3)


# ----------------------------------------------------------------------- CLI
def _cli(*args, timeout=300):
    return subprocess.run([sys.executable, "-m", "timelinerx", *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL,
                          encoding="utf-8", env=ENV, timeout=timeout)


def test_cli_exit_codes(tmp_path, fix):
    assert _cli("import", str(tmp_path / "nope.json")).returncode == 10
    bad = tmp_path / "bad.json"
    bad.write_text("hello", encoding="utf-8")
    assert _cli("import", str(bad)).returncode == 11
    bp = tmp_path / "bad.nrproj"
    bp.write_text("{}", encoding="utf-8")
    assert _cli("render", "--project", str(bp)).returncode == 20
    r = _cli("import", str(fix / "small.json"))
    assert r.returncode == 0 and json.loads(r.stdout)["semantic_points"] == 93
    r = _cli("exit-codes")
    table = json.loads(r.stdout)
    assert table["0"] == ["success"] and "cancelled" in table["60"]


def test_cli_render_json_lines_and_repair(tmp_path, fix):
    pp = tmp_path / "p.nrproj"
    r = _cli("new-project", "--timeline", str(fix / "small.json"), "--out", str(pp), "--name", "Aulia",
             "--resolution", "480p", "--duration", "5", "--map-provider", "plain")
    assert r.returncode == 0, r.stderr
    r = _cli("render", "--project", str(pp), "--output", str(tmp_path / "o.mp4"), "--json", "--no-library")
    assert r.returncode == 0, r.stderr[-1500:]
    lines = [json.loads(l) for l in r.stdout.splitlines()]
    assert lines[-1]["event"] == "result" and Path(lines[-1]["output"]).is_file()
    assert all("event" in l for l in lines)
    r = _cli("repair", str(fix / "broken-truncated.json"), "--out-dir", str(tmp_path / "rep"))
    assert r.returncode == 0 and json.loads(r.stdout)["validation"]["ok"]
    r = _cli("preview", "--project", str(pp), "--out", str(tmp_path / "p.png"), "--preview-resolution", "1080p",
             "--resolution", "2160p")
    assert r.returncode == 0, r.stderr
    im = Image.open(tmp_path / "p.png")
    assert im.size == (1920, 1080)                          # capped at the preview limit
    r = _cli("preview", "--project", str(pp), "--out", str(tmp_path / "p2.png"), "--preview-resolution", "1440p")
    assert r.returncode == 2


def test_cli_scan(tmp_path):
    r = _cli("scan", "--no-hw-test")
    d = json.loads(r.stdout)
    assert d["profile"] in ("Comfortable", "Heavy", "Extreme") and d["labels"]["disclaimer"]


def test_watch_folder_renders_once(tmp_path, fix):
    from timelinerx.pipeline.watch import WatchFolder
    watch = tmp_path / "in"
    watch.mkdir()
    preset = make_project(fix / "small.json", duration_s=5)
    notes = []
    w = WatchFolder(watch, preset, tmp_path / "out", tmp_path / "watch.log", notify=lambda t, m: notes.append(m))
    (watch / "Timeline.json").write_bytes((fix / "small.json").read_bytes())
    assert w.poll_once() == []            # first sighting: wait for a stable size
    out = w.poll_once()
    assert len(out) == 1 and out[0].is_file()
    assert w.poll_once() == []            # never rendered twice
    assert "DONE" in (tmp_path / "watch.log").read_text(encoding="utf-8") and len(notes) == 2


@needs_ffmpeg
def test_hdr_asks_before_sdr_when_no_chain_works(tmp_path, fix, monkeypatch):
    """A build whose zimg rejects every SDR→PQ chain must lead to an explicit HDR→SDR decision,
    never to an FFmpeg crash in the encode step (seen with some Windows FFmpeg builds)."""
    monkeypatch.setattr(ff, "hdr_chain", lambda *a, **k: (None, "no SDR→HDR10 conversion worked (test)"))
    proj = make_project(fix / "small.json", duration_s=5, codec="hevc", hdr=True)
    with pytest.raises(FallbackRequiresConfirmationError):
        RenderJob(proj, tmp_path / "x.mp4", add_to_library=False).run()


@needs_ffmpeg
def test_hdr_encode_failure_asks_then_exports_sdr(tmp_path, fix, monkeypatch):
    """If the verified chain still fails on the real frames, every other chain is tried and then
    the user is asked; with consent the result is a valid SDR video, never a crash."""
    monkeypatch.setattr(ff, "hdr_chain", lambda *a, **k: ("nullsink_that_does_not_exist", ""))
    monkeypatch.setattr(ff, "HDR_CHAINS", ["another_missing_filter"])
    proj = make_project(fix / "small.json", duration_s=5, codec="hevc", hdr=True)
    seen = []
    pol = FallbackPolicy(confirmer=lambda d: seen.append(d) or True)
    res = RenderJob(proj, tmp_path / "s.mp4", add_to_library=False, policy=pol).run()
    assert [d.subsystem for d in seen] == ["hdr"]
    assert (tmp_path / "s.mp4").is_file() and res["output"]
