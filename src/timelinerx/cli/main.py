"""Headless command-line interface (Section VIII.1).

Uses exactly the same pipeline as the GUI. With ``--json`` every progress
event is printed to stdout as one JSON object per line; human-readable
messages go to stderr. Exit codes: 0 success; otherwise the category code of
the error (see ``core/errors.py`` and ``timelinerx exit-codes``).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List, Optional

from .. import APP_NAME, RENDER_ENGINE_VERSION, __version__
from ..core import errors as E
from ..core.fallback import FallbackDecision, FallbackPolicy
from ..utils.cancel import CancelToken


def _print_json(obj) -> None:
    sys.stdout.write(json.dumps(obj, default=str, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _eprint(*a) -> None:
    print(*a, file=sys.stderr, flush=True)


class _Reporter:
    def __init__(self, as_json: bool):
        self.as_json = as_json
        self._last = 0.0
        self._last_node = ""

    def __call__(self, ev: dict) -> None:
        kind = ev.get("event")
        now = time.time()
        if kind == "node_progress" and now - self._last < 0.25:
            return
        self._last = now
        if self.as_json:
            _print_json(ev)
            return
        if kind in ("node_started", "node_cached", "node_done", "node_failed"):
            _eprint(f"[{kind.split('_')[1]:>7}] {ev.get('node')}"
                    + (f" ({ev.get('duration_s', 0):.1f}s)" if kind == "node_done" else "")
                    + (f": {ev.get('error')}" if kind == "node_failed" else ""))
        elif kind == "node_progress":
            eta = ev.get("eta", {})
            e = eta.get("eta_s")
            _eprint(f"  {ev['overall'] * 100:5.1f}%  {ev.get('node')}: {ev.get('message', '')}"
                    + (f"  ETA {int(e // 60)}m{int(e % 60):02d}s" if e is not None else "  ETA estimating")
                    + (f"  {eta['render_fps']} fps" if eta.get("render_fps") else ""))
        elif kind == "fallback":
            d = ev["decision"]
            _eprint(f"[fallback] {d['subsystem']}: {d['requested']} → {d['proposed']} — {d['reason']}")
        elif kind in ("job_started", "job_done", "resume", "warning"):
            _eprint(f"[{kind}] " + json.dumps({k: v for k, v in ev.items()
                                              if k not in ("perf", "eta", "time", "event")}, default=str))


def _confirm_tty(decision: FallbackDecision) -> bool:
    if not sys.stdin.isatty():
        return False
    _eprint(decision.describe())
    # the question goes to stderr: stdout carries only JSON lines in --json mode
    sys.stderr.write("Accept this fallback? [y/N] ")
    sys.stderr.flush()
    try:
        line = sys.stdin.readline()
    except (EOFError, OSError):
        return False
    return line.strip().lower() in ("y", "yes")


def _apply_overrides(proj, a) -> None:
    v = proj.video
    for attr, key in (("resolution", "resolution"), ("fps", "fps"), ("duration_s", "duration"),
                      ("aspect", "aspect"), ("quality", "quality"), ("codec", "codec"),
                      ("encoder", "encoder")):
        val = getattr(a, key, None)
        if val is not None:
            setattr(v, attr, val)
    if getattr(a, "two_pass", False):
        v.two_pass = True
    if getattr(a, "hdr", False):
        v.hdr = True
    if getattr(a, "theme", None):
        proj.visual.theme = a.theme
    if getattr(a, "camera", None):
        from ..camera.planner import LEGACY_CAMERA_MODES
        proj.camera.mode = LEGACY_CAMERA_MODES.get(a.camera, a.camera)
    if getattr(a, "long_trip_detection", None):
        proj.journey.trip_detection = a.long_trip_detection
    if getattr(a, "local_framing", None):
        proj.camera.local_framing = a.local_framing
    if getattr(a, "long_trip_pacing", None):
        proj.camera.compression = a.long_trip_pacing
    if getattr(a, "motion_blur", False):
        v.motion_blur = True
    if getattr(a, "unit", None):
        proj.title.unit = a.unit
    if getattr(a, "map_provider", None):
        proj.visual.map_provider = a.map_provider
    if getattr(a, "offline", False):
        proj.visual.offline = True


def cmd_render(a) -> int:
    from ..pipeline.render_job import RenderJob, default_output_path
    from ..projects.project import Project
    proj = Project.load(a.project)
    _apply_overrides(proj, a)
    out = Path(a.output) if a.output else default_output_path(proj)
    policy = FallbackPolicy(confirmer=_confirm_tty, accept_all=a.accept_fallback)
    cancel = CancelToken()
    job = RenderJob(proj, out, policy=policy, listener=_Reporter(a.json), cancel=cancel,
                    overwrite=a.overwrite, allow_placeholder_tiles=True if a.allow_placeholder_tiles else None,
                    segment_seconds=a.segment_seconds, ffmpeg_path=a.ffmpeg,
                    keep_job_files=a.keep_job_files, add_to_library=not a.no_library)
    try:
        res = job.run()
    except KeyboardInterrupt:
        cancel.cancel("interrupted")
        _eprint(f"Interrupted. Resume with: timelinerx resume --job-dir \"{job.job_dir}\"")
        return E.RenderCancelledError.exit_code
    if a.json:
        _print_json({"event": "result", **res})
    else:
        _eprint(f"Done: {res['output']}")
    return 0


def cmd_resume(a) -> int:
    from ..pipeline.render_job import RenderJob
    policy = FallbackPolicy(confirmer=_confirm_tty, accept_all=a.accept_fallback)
    job = RenderJob.resume(a.job_dir, policy=policy, listener=_Reporter(a.json))
    res = job.run()
    if a.json:
        _print_json({"event": "result", **res})
    else:
        _eprint(f"Done: {res['output']}")
    return 0


def cmd_import(a) -> int:
    from ..timeline.parser import load_timeline
    t0 = time.perf_counter()
    tl = load_timeline(a.timeline, progress=(lambda f, m: _eprint(f"  {f * 100:5.1f}% {m}")) if a.verbose else None)
    rng = tl.date_range()
    out = {"file": a.timeline, "sha256": tl.source_sha256, "diagnostics": tl.diagnostics.to_dict(),
           "semantic_points": len(tl.semantic), "raw_points": len(tl.raw),
           "date_range": [str(rng[0]), str(rng[1])] if rng else None,
           "seconds": round(time.perf_counter() - t0, 3)}
    _print_json(out)
    return 0


def cmd_repair(a) -> int:
    from ..repair.engine import analyze, apply_plan
    plan = analyze(a.timeline, progress=(lambda n: _eprint(f"pass: {n}")) if a.verbose else None)
    if a.dry_run or plan.document is None:
        _print_json({"summary": plan.summary(), "structurally_valid": plan.structurally_valid,
                     "findings": [f.__dict__ for f in plan.findings],
                     "actions": [x.to_dict() for x in plan.actions]})
        return 0 if plan.document is not None else E.ImportFailedError.exit_code
    res = apply_plan(plan, accept_ids=set(a.accept or []), reject_ids=set(a.reject or []),
                     accept_low=a.accept_low, output_dir=Path(a.out_dir) if a.out_dir else None)
    _print_json({"fixed": str(res.fixed_path), "report_html": str(res.report_html),
                 "report_json": str(res.report_json), "log": str(res.log_path),
                 "applied": len(res.applied), "not_applied": len(res.rejected), "validation": res.validation})
    return 0 if res.validation.get("ok") else E.VerificationFailedError.exit_code


def cmd_scan(a) -> int:
    from ..environment.scan import scan
    rep = scan(ffmpeg_path=a.ffmpeg, verify_hw=not a.no_hw_test)
    _print_json(rep.to_dict())
    return 0


def cmd_new_project(a) -> int:
    from ..projects.project import Project, TimelineRef
    from ..timeline.parser import sha256_file
    p = Path(a.timeline).resolve()
    if not p.is_file():
        raise E.ImportFailedError(f"Timeline not found: {p}")
    proj = Project(name=a.name or p.stem, timeline=TimelineRef(str(p), sha256_file(p), p.stat().st_size))
    proj.period.start, proj.period.end = a.start, a.end
    if a.name:
        proj.title.name = a.name
    _apply_overrides(proj, a)
    errs = proj.validate()
    if errs:
        raise E.UsageError("; ".join(errs))
    out = proj.save(a.out)
    _print_json({"project": str(out)})
    return 0


def cmd_preview(a) -> int:
    from ..projects.project import PREVIEW_RESOLUTIONS, Project
    from ..pipeline.preview import PreviewSession
    proj = Project.load(a.project)
    _apply_overrides(proj, a)
    if a.preview_resolution not in PREVIEW_RESOLUTIONS:
        raise E.UsageError(f"Preview is limited to {', '.join(PREVIEW_RESOLUTIONS)}.")
    s = PreviewSession(proj, a.preview_resolution, allow_placeholder=a.allow_placeholder_tiles)
    img = s.render_frame(a.frame if a.frame >= 0 else s.plan.frame_count // 2)
    img.save(a.out)
    _print_json({"preview": a.out, "frame_count": s.plan.frame_count, "size": [img.width(), img.height()]})
    return 0


def cmd_compare(a) -> int:
    from ..pipeline.compare import compare
    out = compare(Path(a.a), Path(a.b), Path(a.out), a.mode, a.label_a, a.label_b, a.ffmpeg,
                  title=a.title)
    _print_json({"output": str(out)})
    return 0


def cmd_watch(a) -> int:
    from ..pipeline.watch import WatchFolder
    from ..projects.project import Project
    from ..utils.paths import logs_dir
    w = WatchFolder(Path(a.folder), Project.load(a.preset), Path(a.out_dir), logs_dir() / "watch.log",
                    notify=lambda t, m: _eprint(f"[notify] {m}"), accept_fallback=a.accept_fallback,
                    poll_s=a.poll)
    if a.once:
        _print_json({"rendered": [str(p) for p in w.poll_once() + w.poll_once()]})
        return 0
    try:
        w.run_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_themes(a) -> int:
    from ..plugins.loader import load_plugins
    rep = load_plugins(load_python=a.load_python_plugins)
    from ..rendering.styles import load_themes
    _print_json({"themes": {k: {"name": t.name, "base": t.base, "source": t.source,
                                "experimental": t.experimental} for k, t in load_themes().items()},
                 "plugin_providers": list(rep.providers), "plugin_errors": rep.errors})
    return 0


def cmd_jobs(a) -> int:
    import shutil
    from ..utils.paths import jobs_dir
    rows = []
    for d in sorted(jobs_dir().iterdir()):
        if not (d / "job.json").is_file():
            continue
        try:
            m = json.loads((d / "job.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        rows.append({"job_dir": str(d), "output": m.get("output"), "created_at": m.get("created_at"),
                     "size_mb": round(size / 2 ** 20, 1)})
        if a.clean:
            shutil.rmtree(d, ignore_errors=True)
    _print_json({"jobs": rows, "cleaned": bool(a.clean)})
    return 0


def cmd_tutorial(a) -> int:
    from ..pipeline.tutorial_export import export_tutorial
    out = export_tutorial(Path(a.out), a.platform, a.lang, theme=a.theme, ffmpeg_path=a.ffmpeg,
                          progress=lambda f: _eprint(f"  {f * 100:5.1f}%"))
    _print_json({"tutorial": str(out)})
    return 0


def cmd_exit_codes(a) -> int:
    codes = {}
    for name in dir(E):
        c = getattr(E, name)
        if isinstance(c, type) and issubclass(c, E.TimelinerXError):
            codes[c.exit_code] = codes.get(c.exit_code, []) + [c.category]
    codes[0] = ["success"]
    _print_json({str(k): v for k, v in sorted(codes.items())})
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="timelinerx",
                                description=f"{APP_NAME} {__version__} ({RENDER_ENGINE_VERSION}) — headless mode")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__} ({RENDER_ENGINE_VERSION})")
    p.add_argument("--carto-key", metavar="KEY",
                   help="CARTO basemap API key for this run (default: the key saved in the app's "
                        "Settings, then TIMELINERX_CARTO_KEY / CARTO_BASEMAP_API_KEY)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def video_opts(sp):
        sp.add_argument("--resolution", choices=["480p", "720p", "1080p", "1440p", "2160p", "4K", "8K"])
        sp.add_argument("--fps", type=int, choices=[24, 30, 60])
        sp.add_argument("--duration", type=float, help="seconds")
        sp.add_argument("--aspect", choices=["16:9", "9:16", "1:1"])
        sp.add_argument("--quality", choices=["draft", "standard", "high", "cinematic"])
        sp.add_argument("--codec", choices=["h264", "hevc"])
        sp.add_argument("--encoder", choices=["auto", "nvenc", "qsv", "amf", "software"])
        sp.add_argument("--two-pass", action="store_true")
        sp.add_argument("--hdr", action="store_true", help="experimental HDR10 export (HEVC)")
        sp.add_argument("--theme")
        sp.add_argument("--zoom-style", "--camera", dest="camera",
                        choices=["fixed", "balanced", "active", "close_up", "steady", "dynamic"],
                        help="zoom style (0.x names steady/dynamic are accepted)")
        sp.add_argument("--long-trip-detection", choices=["conservative", "balanced", "sensitive"])
        sp.add_argument("--local-framing", choices=["off", "balanced", "close"])
        sp.add_argument("--long-trip-pacing", choices=["natural", "balanced", "faster", "fastest"])
        sp.add_argument("--motion-blur", action="store_true", help="180° shutter motion blur (slower render)")
        sp.add_argument("--unit", choices=["km", "mi"], help="distance unit in titles")
        sp.add_argument("--map-provider")
        sp.add_argument("--offline", action="store_true", help="use cached tiles only")

    r = sub.add_parser("render", help="render a project to MP4")
    r.add_argument("--project", required=True)
    r.add_argument("--output")
    video_opts(r)
    r.add_argument("--accept-fallback", action="store_true",
                   help="consent in advance to any explicit fallback (encoder, HDR→SDR, placeholders)")
    r.add_argument("--allow-placeholder-tiles", action="store_true")
    r.add_argument("--overwrite", action="store_true")
    r.add_argument("--segment-seconds", type=float, default=4.0)
    r.add_argument("--ffmpeg")
    r.add_argument("--keep-job-files", action="store_true")
    r.add_argument("--no-library", action="store_true")
    r.add_argument("--json", action="store_true", help="JSON-lines progress on stdout")
    r.set_defaults(fn=cmd_render)

    rs = sub.add_parser("resume", help="resume an interrupted render from its job directory")
    rs.add_argument("--job-dir", required=True)
    rs.add_argument("--accept-fallback", action="store_true")
    rs.add_argument("--json", action="store_true")
    rs.set_defaults(fn=cmd_resume)

    i = sub.add_parser("import", help="import and diagnose a Timeline export")
    i.add_argument("timeline")
    i.add_argument("--verbose", action="store_true")
    i.set_defaults(fn=cmd_import)

    rp = sub.add_parser("repair", help="forensic repair of a damaged Timeline export")
    rp.add_argument("timeline")
    rp.add_argument("--dry-run", action="store_true", help="only list proposed actions")
    rp.add_argument("--accept-low", action="store_true", help="also apply LOW confidence actions")
    rp.add_argument("--accept", nargs="*", help="action ids to apply in addition")
    rp.add_argument("--reject", nargs="*", help="action ids to skip")
    rp.add_argument("--out-dir")
    rp.add_argument("--verbose", action="store_true")
    rp.set_defaults(fn=cmd_repair)

    sc = sub.add_parser("scan", help="environment scan and recommendations")
    sc.add_argument("--ffmpeg")
    sc.add_argument("--no-hw-test", action="store_true")
    sc.set_defaults(fn=cmd_scan)

    np_ = sub.add_parser("new-project", help="create a .nrproj for a Timeline")
    np_.add_argument("--timeline", required=True)
    np_.add_argument("--out", required=True)
    np_.add_argument("--name")
    np_.add_argument("--start", help="YYYY-MM-DD")
    np_.add_argument("--end", help="YYYY-MM-DD")
    video_opts(np_)
    np_.set_defaults(fn=cmd_new_project)

    pv = sub.add_parser("preview", help="render one preview frame to PNG (480p/720p/1080p only)")
    pv.add_argument("--project", required=True)
    pv.add_argument("--frame", type=int, default=-1)
    pv.add_argument("--preview-resolution", default="720p")
    pv.add_argument("--allow-placeholder-tiles", action="store_true")
    pv.add_argument("--out", required=True)
    video_opts(pv)
    pv.set_defaults(fn=cmd_preview)

    cp = sub.add_parser("compare", help="combine two renders (split screen or sequential)")
    cp.add_argument("--a", required=True)
    cp.add_argument("--b", required=True)
    cp.add_argument("--out", required=True)
    cp.add_argument("--mode", choices=["split", "sequential"], default="split")
    cp.add_argument("--label-a", default="A")
    cp.add_argument("--label-b", default="B")
    cp.add_argument("--title", default="Comparison")
    cp.add_argument("--ffmpeg")
    cp.set_defaults(fn=cmd_compare)

    w = sub.add_parser("watch", help="watch a folder and render new Timeline exports with a preset")
    w.add_argument("--folder", required=True)
    w.add_argument("--preset", required=True, help="a .nrproj used as the preset")
    w.add_argument("--out-dir", required=True)
    w.add_argument("--poll", type=float, default=5.0)
    w.add_argument("--once", action="store_true")
    w.add_argument("--accept-fallback", action="store_true")
    w.set_defaults(fn=cmd_watch)

    t = sub.add_parser("themes", help="list themes and plugin status")
    t.add_argument("--load-python-plugins", action="store_true")
    t.set_defaults(fn=cmd_themes)

    j = sub.add_parser("jobs", help="list (or --clean) resumable render jobs")
    j.add_argument("--clean", action="store_true")
    j.set_defaults(fn=cmd_jobs)

    tu = sub.add_parser("tutorial", help="render the import tutorial motion graphic to MP4")
    tu.add_argument("--out", required=True)
    tu.add_argument("--platform", choices=["android", "iphone"], default="android")
    tu.add_argument("--lang", help="interface language code (default: English)")
    tu.add_argument("--theme", choices=["dark", "light"], default="dark")
    tu.add_argument("--ffmpeg")
    tu.set_defaults(fn=cmd_tutorial)

    ec = sub.add_parser("exit-codes", help="print the exit-code table")
    ec.set_defaults(fn=cmd_exit_codes)
    return p


def _utf8_streams() -> None:
    """Windows consoles/pipes default to a legacy code page; JSON lines are always UTF-8."""
    for name in ("stdout", "stderr"):
        st = getattr(sys, name, None)
        if st is not None and hasattr(st, "reconfigure"):
            try:
                st.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _init_keys(a) -> None:
    from ..maps.keys import set_user_key
    if getattr(a, "carto_key", None):
        set_user_key(a.carto_key)
        return
    try:
        from ..storage.settings import AppSettings
        set_user_key(AppSettings.load().maps.carto_api_key)
    except Exception:  # noqa: BLE001 — unreadable settings never block headless use
        pass


def main(argv: Optional[List[str]] = None) -> int:
    _utf8_streams()
    parser = build_parser()
    a = parser.parse_args(argv)
    _init_keys(a)
    if a.cmd in ("render", "resume", "preview", "compare", "watch", "tutorial"):
        from ..rendering.qt import ensure_qt_app
        ensure_qt_app()          # Qt must live on the main thread; workers only paint QImages
    try:
        return int(a.fn(a) or 0)
    except E.TimelinerXError as e:
        if getattr(a, "json", False):
            _print_json({"event": "error", **e.to_dict()})
        _eprint(f"Error [{e.category}]: {e}" + (f"\nHint: {e.hint}" if e.hint else ""))
        return e.exit_code
    except KeyboardInterrupt:
        return E.RenderCancelledError.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
