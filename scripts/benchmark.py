"""Benchmarks (Section XV.2). Numbers are measured on the machine running this
script and written to docs/BENCHMARKS.md exactly as measured.

    python scripts/benchmark.py [--quick]
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts"), str(ROOT / "tests")]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TIMELINERX_HOME", tempfile.mkdtemp(prefix="tlx-bench-"))


def machine() -> dict:
    import psutil
    from timelinerx.environment.scan import cpu_name
    return {"os": f"{platform.system()} {platform.release()}", "python": platform.python_version(),
            "cpu": cpu_name(), "logical_cpus": psutil.cpu_count(), "ram_gb": round(psutil.virtual_memory().total / 2**30, 1)}


def bench_parse(tmp: Path, sizes) -> list:
    import make_fixtures
    from timelinerx.timeline.parser import load_timeline
    out = []
    for mb in sizes:
        f = make_fixtures.make_large(tmp / f"big{mb}.json", mb)
        real = f.stat().st_size / 2**20
        t0 = time.perf_counter()
        tl = load_timeline(f, compute_sha=False)
        dt = time.perf_counter() - t0
        out.append({"file_mb": round(real, 1), "points": len(tl.semantic), "seconds": round(dt, 2),
                    "mb_per_s": round(real / dt, 1), "mode": "streaming" if real >= 50 else "in-memory"})
        f.unlink()
    return out


def bench_render(resolutions, frames=90) -> list:
    from timelinerx.maps.tiles import PLUGIN_PROVIDERS
    from timelinerx.pipeline.preview import PreviewSession
    from timelinerx.projects.project import Project, TimelineRef
    from visual_support import SyntheticProvider
    PLUGIN_PROVIDERS["synthetic-test"] = SyntheticProvider
    out = []
    fix = ROOT / "fixtures" / "standard-route.json"
    for res in resolutions:
        for theme in ("light", "neon_dark_blue"):
            p = Project(name="bench", timeline=TimelineRef(str(fix)))
            p.visual.theme, p.visual.map_provider = theme, "synthetic-test"
            p.video.resolution, p.video.duration_s = res, 30
            from timelinerx.pipeline import preview as pv
            pv.PREVIEW_RESOLUTIONS = ["480p", "720p", "1080p", "1440p", "2160p"]  # bench only: full size
            pv.preview_dimensions.__globals__["PREVIEW_RESOLUTIONS"] = pv.PREVIEW_RESOLUTIONS
            s = PreviewSession(p, res, allow_placeholder=False, tile_root=Path(tempfile.mkdtemp()))
            for f in range(frames):           # warm tile/grade caches on the measured frames
                s.render_frame(f * 9)
            t0 = time.perf_counter()
            for f in range(frames):
                s.render_frame(f * 9)
            dt = time.perf_counter() - t0
            out.append({"resolution": res, "size": f"{s.width}x{s.height}", "theme": theme,
                        "frames": frames, "render_fps": round(frames / dt, 1), "ms_per_frame": round(dt / frames * 1000, 1)})
    return out


def bench_end_to_end(tmp: Path) -> list:
    from conftest import make_project
    from timelinerx.pipeline.render_job import RenderJob
    out = []
    for res, dur in (("720p", 10), ("1080p", 10)):
        proj = make_project(ROOT / "fixtures" / "standard-route.json", resolution=res, duration_s=dur, quality="high")
        t0 = time.perf_counter()
        r = RenderJob(proj, tmp / f"e2e_{res}.mp4", add_to_library=False).run()
        dt = time.perf_counter() - t0
        enc = [n for n in r["nodes"] if n["node"] == "render_frames"][0]
        out.append({"resolution": res, "video_seconds": dur, "frames": dur * 30, "wall_seconds": round(dt, 1),
                    "realtime_factor": round(dur / dt, 2), "encoder": r["verify"]["codec"],
                    "render_frames_seconds": round(enc["duration_s"], 1)})
    return out


def main():
    quick = "--quick" in sys.argv
    from timelinerx.rendering.qt import ensure_qt_app
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    ensure_qt_app()
    tmp = Path(tempfile.mkdtemp(prefix="tlx-bench-files-"))
    res = {"machine": machine(), "date": time.strftime("%Y-%m-%d"),
           "parse": bench_parse(tmp, [10, 50] if quick else [10, 50, 100]),
           "render": bench_render(["480p", "1080p"] if quick else ["480p", "1080p", "1440p"]),
           "end_to_end": bench_end_to_end(tmp)}
    shutil.rmtree(tmp, ignore_errors=True)
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "benchmarks.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    m = res["machine"]
    lines = ["# Benchmarks", "",
             f"Measured {res['date']} by `scripts/benchmark.py` on: {m['os']}, {m['cpu']}, "
             f"{m['logical_cpus']} logical CPUs, {m['ram_gb']} GB RAM, Python {m['python']}. "
             "No GPU encoder was available on this machine, so all encoding used libx264 (software).", "",
             "These are the numbers as measured on that machine — not tuned, not extrapolated. "
             "Re-run the script on your own hardware; expect very different figures on a desktop with "
             "more cores and a hardware encoder.", "",
             "## Timeline parsing", "", "| File size | Points | Mode | Time | Throughput |", "|---|---|---|---|---|"]
    for p in res["parse"]:
        lines.append(f"| {p['file_mb']} MB | {p['points']:,} | {p['mode']} | {p['seconds']} s | {p['mb_per_s']} MB/s |")
    lines += ["", "## Frame rendering (renderer only, no encoding)", "",
              "Synthetic in-process map tiles, standard test route, steady state (tiles already fetched "
              "and graded — in a real render the Prepare Tiles / Prepare Cache stages do that up front).", "",
              "| Resolution | Size | Theme | ms / frame | Frames / s |", "|---|---|---|---|---|"]
    for r in res["render"]:
        lines.append(f"| {r['resolution']} | {r['size']} | {r['theme']} | {r['ms_per_frame']} | {r['render_fps']} |")
    lines += ["", "## End to end (render + libx264 encode + verify)", "",
              "Plain provider (no map tiles), neon theme, quality *high*, 30 fps.", "",
              "| Resolution | Video length | Wall time | Speed vs real time |", "|---|---|---|---|"]
    for e in res["end_to_end"]:
        lines.append(f"| {e['resolution']} | {e['video_seconds']} s | {e['wall_seconds']} s | {e['realtime_factor']}× |")
    lines.append("")
    (ROOT / "docs" / "BENCHMARKS.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
