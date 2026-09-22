"""The render job: project → verified MP4 in the library, as a resumable DAG.

Nodes (dependencies in brackets)::

    preflight            []                          FFmpeg probe, encoder matrix, HDR/2-pass gating, disk space
    analyze_audio        []                          optional local onset analysis
    import               []                          Timeline parse (own import checkpoints for big files)
    normalize            [import]                    period + route source
    filter               [normalize]                 GPS excursion filter
    build_journey        [filter]                    projection, distances, trip legs
    plan_camera          [build_journey, analyze_audio]
    prepare_tiles        [plan_camera]               tile set + prefetch; missing tiles → explicit consent
    prepare_cache        [prepare_tiles]             grade every needed tile once (disk graded cache)
    render_frames        [prepare_cache, preflight]  segmented render+encode; each segment is a checkpoint
    encode               [render_frames, analyze_audio, preflight]   concat/mux (or 2-pass / HDR re-encode)
    verify               [encode]                    ffprobe dims/fps/frames + decode test
    finalize_metadata    [verify]                    sidecar .nrmeta.json
    generate_thumbnail   [verify]
    add_to_library       [finalize_metadata, generate_thumbnail]

``import``, ``preflight`` and ``analyze_audio`` run in parallel; so do
``finalize_metadata`` and ``generate_thumbnail``.

Resume: every node's result is checkpointed under the job directory. After a
crash or power loss, ``RenderJob.resume(job_dir)`` re-runs only nodes whose
checkpoints are missing or invalid; ``render_frames`` additionally skips all
segments already committed, so interrupting ``encode`` never re-renders
frames.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from .. import RENDER_ENGINE_VERSION, __version__
from ..audio.beat import analyze_file, audio_filter, nearest_onset
from ..camera.planner import CameraConfig, FramePlan, Keyframe, jerk_report, plan_frames
from ..core.errors import (FallbackRequiresConfirmationError, InsufficientStorageError, TimelinerXError,
                           ProjectError, RenderCancelledError, RenderFailedError, TileProviderError,
                           UsageError, VerificationFailedError)
from ..core.fallback import FallbackDecision, FallbackPolicy
from ..encoding import ffmpeg as ff
from ..journeys.journey import (Journey, JourneyConfig, filter_route_points, journey_from_columns,
                                select_route_points)
from ..maps.compositor import MapCompositor, tiles_for_plan
from ..maps.tiles import PlainProvider, TileCache, cache_namespace, make_provider, missing_key
from ..projects.project import Project
from ..rendering.frame_renderer import FrameRenderer, build_overlays
from ..rendering.styles import get_theme
from ..storage.library import Library, VideoEntry
from ..timeline.model import PointColumns, Timeline
from ..timeline.parser import load_timeline
from ..utils.cancel import CancelToken
from ..utils.paths import cache_dir, jobs_dir, safe_filename
from .dag import Node, RenderGraph, fingerprint_of
from .progress import EtaEstimator, PerformanceMonitor

Listener = Callable[[dict], None]


def provider_spec(project: Project) -> str:
    theme = get_theme(project.visual.theme)
    spec = project.visual.map_provider
    if spec == "carto":
        return "carto-dark" if theme.base == "dark" else "carto-light"
    return spec


def graded_cache_dir(project: Project, tile_root: Optional[Path] = None) -> Path:
    t = get_theme(project.visual.theme)
    spec = provider_spec(project)
    try:
        ns = cache_namespace(make_provider(spec))
    except TimelinerXError:
        ns = spec
    key = fingerprint_of(t.grading, ns)[:16]
    return (tile_root or cache_dir() / "tiles").parent / "graded" / key


def build_renderer(project: Project, journey: Journey, plan: FramePlan, *, allow_placeholder: bool,
                   graded_dir: Optional[Path] = None, offline: Optional[bool] = None,
                   tile_root: Optional[Path] = None, force_plain: bool = False,
                   motion_blur: Optional[bool] = None):
    theme = get_theme(project.visual.theme)
    if graded_dir is None:
        graded_dir = graded_cache_dir(project, tile_root)
    provider = PlainProvider() if force_plain else make_provider(provider_spec(project))
    cache = None if isinstance(provider, PlainProvider) else TileCache(
        provider, tile_root or (cache_dir() / "tiles"),
        offline=project.visual.offline if offline is None else offline)
    comp = MapCompositor(cache, theme, allow_placeholder=allow_placeholder, graded_dir=graded_dir)
    renderer = FrameRenderer(journey, plan, theme, comp, project.visual.trail, project.title,
                             provider.attribution(), vignette=project.visual.vignette,
                             grain=project.visual.grain,
                             motion_blur=project.video.motion_blur if motion_blur is None else motion_blur)
    return renderer, comp, cache


def camera_config(project: Project) -> CameraConfig:
    c = project.camera
    return CameraConfig(mode=c.mode, composition=c.composition, local_framing=c.local_framing,
                        pacing=c.pacing, compression=c.compression,
                        trip_detection=project.journey.trip_detection, smoothing=c.smoothing,
                        anticipation=c.anticipation, intro_s=c.intro_s,
                        outro_transition_s=c.outro_transition_s, outro_hold_s=c.outro_hold_s,
                        keyframes=[Keyframe.from_dict(k) for k in c.keyframes],
                        overview_bottom_reserve=0.24 if project.title.ending_title else 0.0)


def journey_config(project: Project) -> JourneyConfig:
    s, e = project.period.dates()
    return JourneyConfig(start_date=s, end_date=e, route_source=project.journey.route_source,
                         outlier_filter=project.journey.outlier_filter,
                         trip_detection=project.journey.trip_detection)


def _save_cols(path: Path, cols: PointColumns) -> None:
    tmp = path.with_suffix(".tmp.npz")
    np.savez(tmp, **cols.to_npz_dict("c"))
    os.replace(tmp, path)


def _load_cols(path: Path) -> PointColumns:
    with np.load(path) as d:
        return PointColumns.from_npz(d, "c")


class RenderJob:
    def __init__(self, project: Project, output_path: str | Path, *, policy: Optional[FallbackPolicy] = None,
                 listener: Optional[Listener] = None, cancel: Optional[CancelToken] = None,
                 job_dir: Optional[Path] = None, library: Optional[Library] = None,
                 overwrite: bool = False, allow_placeholder_tiles: Optional[bool] = None,
                 segment_seconds: float = 4.0, ffmpeg_path: Optional[str] = None,
                 keep_job_files: bool = False, tile_root: Optional[Path] = None,
                 add_to_library: bool = True):
        errs = project.validate()
        if errs:
            raise UsageError("Project settings are invalid:\n- " + "\n- ".join(errs))
        if not project.timeline.path:
            raise ProjectError("The project has no Timeline file.")
        self.project = project
        self.output = Path(output_path).resolve()
        if self.output.suffix.lower() != ".mp4":
            raise UsageError("Output file must end in .mp4")
        self.policy = policy or FallbackPolicy()
        self.listener = listener
        self.cancel = cancel or CancelToken()
        self.library = library
        self.overwrite = overwrite
        self.allow_placeholder = allow_placeholder_tiles
        self.segment_seconds = max(1.0, float(segment_seconds))
        self.ffmpeg_path = ffmpeg_path
        self.keep_job_files = keep_job_files
        self.tile_root = tile_root
        self.add_to_lib = add_to_library
        self.width, self.height = project.video.dimensions()
        self.fps = project.video.fps
        self.total_frames = max(2, int(round(project.video.duration_s * self.fps)))
        self.job_id = fingerprint_of(project.to_dict(), str(self.output), RENDER_ENGINE_VERSION)[:16]
        self.job_dir = Path(job_dir) if job_dir else jobs_dir() / self.job_id
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self.eta = EtaEstimator(self.total_frames)
        self.perf = PerformanceMonitor()
        self.graph: Optional[RenderGraph] = None
        self._write_manifest()

    # ------------------------------------------------------------ manifest
    def _write_manifest(self) -> None:
        m = {"job_id": self.job_id, "output": str(self.output), "project": self.project.to_dict(),
             "overwrite": self.overwrite, "segment_seconds": self.segment_seconds,
             "ffmpeg_path": self.ffmpeg_path, "keep_job_files": self.keep_job_files,
             "allow_placeholder_tiles": self.allow_placeholder,
             "created_at": datetime.now(timezone.utc).isoformat(),
             "render_engine_version": RENDER_ENGINE_VERSION}
        p = self.job_dir / "job.json"
        if p.is_file():
            try:
                old = json.loads(p.read_text(encoding="utf-8"))
                m["created_at"] = old.get("created_at", m["created_at"])
            except (OSError, json.JSONDecodeError):
                pass
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, indent=1), encoding="utf-8")
        os.replace(tmp, p)

    @classmethod
    def resume(cls, job_dir: str | Path, **kw) -> "RenderJob":
        jd = Path(job_dir)
        try:
            m = json.loads((jd / "job.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise ProjectError(f"{jd} is not a resumable render job: {e}") from e
        if m.get("render_engine_version") != RENDER_ENGINE_VERSION:
            raise ProjectError("This job was started by a different renderer version and cannot be "
                               "resumed deterministically. Start a new render.")
        proj = Project.from_dict(m["project"])
        opts = dict(job_dir=jd, overwrite=m.get("overwrite", False),
                    segment_seconds=m.get("segment_seconds", 4.0), ffmpeg_path=m.get("ffmpeg_path"),
                    keep_job_files=m.get("keep_job_files", False),
                    allow_placeholder_tiles=m.get("allow_placeholder_tiles"))
        opts.update(kw)
        return cls(proj, m["output"], **opts)

    # --------------------------------------------------------------- events
    def _emit(self, ev: dict) -> None:
        if self.listener is None:
            return
        ev = dict(ev)
        ev.setdefault("job_id", self.job_id)
        ev["perf"] = self.perf.latest
        ev["eta"] = self.eta.to_dict()
        try:
            self.listener(ev)
        except Exception:  # noqa: BLE001
            pass

    def _decision(self, d: FallbackDecision) -> FallbackDecision:
        r = self.policy.resolve(d)
        self._emit({"event": "fallback", "decision": d.to_dict()})
        return r

    # ------------------------------------------------------------------ run
    def run(self) -> Dict[str, Any]:
        g = RenderGraph(self.job_dir, self.cancel, listener=self._emit, max_workers=3)
        self.graph = g
        P = self.project
        jd = self.job_dir
        proj_fp = P.to_dict()
        tl_fp = {"path": P.timeline.path, "sha": P.timeline.sha256}

        g.add(Node("preflight", self._n_preflight, [], lambda: fingerprint_of(
            P.video.__dict__, str(self.output), self.overwrite, self.ffmpeg_path), weight=0.5,
            label="Preflight", checkpoint=False))
        g.add(Node("analyze_audio", self._n_audio, [], lambda: fingerprint_of(P.audio.__dict__),
                   weight=0.3, label="Analyze audio"))
        g.add(Node("import", self._n_import, [], lambda: fingerprint_of(tl_fp), weight=2.0,
                   label="Import", validate=lambda r: (jd / "timeline.npz").is_file()))
        g.add(Node("normalize", self._n_normalize, ["import"], lambda: fingerprint_of(
            P.period.__dict__, P.journey.route_source), weight=0.3, label="Normalize",
            validate=lambda r: (jd / "normalized.npz").is_file()))
        g.add(Node("filter", self._n_filter, ["normalize"], lambda: fingerprint_of(P.journey.outlier_filter),
                   weight=0.3, label="Filter", validate=lambda r: (jd / "filtered.npz").is_file()))
        g.add(Node("build_journey", self._n_journey, ["filter"], lambda: fingerprint_of(
            P.journey.trip_detection), weight=0.3, label="Build journey"))
        g.add(Node("plan_camera", self._n_camera, ["build_journey", "analyze_audio"], lambda: fingerprint_of(
            P.camera.__dict__, P.video.__dict__, P.title.ending_title, RENDER_ENGINE_VERSION),
            weight=0.5, label="Plan camera", validate=lambda r: (jd / "camera.npz").is_file()))
        g.add(Node("prepare_tiles", self._n_tiles, ["plan_camera"], lambda: fingerprint_of(
            provider_spec(P), P.visual.offline, self.allow_placeholder), weight=2.0, label="Prepare tiles"))
        g.add(Node("prepare_cache", self._n_cache, ["prepare_tiles"], lambda: fingerprint_of(
            P.visual.theme, provider_spec(P)), weight=1.0, label="Prepare cache"))
        g.add(Node("render_frames", self._n_render, ["prepare_cache", "preflight", "build_journey"],
                   lambda: fingerprint_of(proj_fp, self.segment_seconds, RENDER_ENGINE_VERSION),
                   weight=30.0, label="Render frames", validate=self._segments_valid))
        g.add(Node("encode", self._n_encode, ["render_frames", "analyze_audio", "preflight"],
                   lambda: fingerprint_of(proj_fp, str(self.output)), weight=4.0, label="Encode",
                   validate=lambda r: bool(r) and Path(r["output"]).is_file()))
        g.add(Node("verify", self._n_verify, ["encode"], lambda: fingerprint_of(str(self.output)),
                   weight=1.0, label="Verify", checkpoint=False))
        g.add(Node("finalize_metadata", self._n_meta, ["verify"], lambda: fingerprint_of(proj_fp),
                   weight=0.2, label="Finalize metadata", checkpoint=False))
        g.add(Node("generate_thumbnail", self._n_thumb, ["verify"], lambda: fingerprint_of(str(self.output)),
                   weight=0.3, label="Thumbnail", checkpoint=False))
        g.add(Node("add_to_library", self._n_library, ["finalize_metadata", "generate_thumbnail"],
                   lambda: "", weight=0.2, label="Add to library", checkpoint=False))

        self.perf.start()
        self._emit({"event": "job_started", "job_dir": str(jd), "output": str(self.output),
                    "width": self.width, "height": self.height, "fps": self.fps,
                    "frames": self.total_frames})
        try:
            g.run()
        except RenderCancelledError:
            self._emit({"event": "job_cancelled"})
            raise
        except BaseException as e:
            self._emit({"event": "job_failed", "error": str(e),
                        "category": getattr(e, "category", "internal")})
            raise
        finally:
            self.perf.stop()
        result = {"output": str(self.output), "job_dir": str(jd), "nodes": g.snapshot(),
                  "fallbacks": [d.to_dict() for d in self.policy.applied],
                  "verify": g.states["verify"].result, "library_id": g.states["add_to_library"].result}
        if not self.keep_job_files:
            shutil.rmtree(jd, ignore_errors=True)
        self._emit({"event": "job_done", "output": str(self.output)})
        return result

    def discard(self) -> None:
        """Delete all temporary files of this job (used after an explicit cancel)."""
        tmp = self.output.with_name(self.output.stem + ".nrpart.mp4")
        for p in (tmp,):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        shutil.rmtree(self.job_dir, ignore_errors=True)

    # ============================================================ nodes
    def _n_preflight(self, ctx) -> dict:
        P = self.project
        prov = make_provider(provider_spec(P))
        if missing_key(prov):
            raise TileProviderError(
                f"{prov.name} needs an API key. Without one CARTO stamps every map tile with "
                "\"API KEY REQUIRED\", so TimelinerX will not render with it.",
                hint="Add your free key in Settings → Maps → CARTO API key (get one at "
                     "https://carto.com/basemaps/apikey/), or choose an offline MBTiles map or "
                     "the Plain background in Visual Settings.")
        info = ff.probe(self.ffmpeg_path)
        if not info.ffprobe:
            raise TimelinerXError("ffprobe was not found next to ffmpeg; outputs cannot be verified.",
                                 hint="Install a complete FFmpeg build (ffmpeg + ffprobe).")
        choice = ff.select_encoder(info, P.video.codec, P.video.encoder, self.width, self.height,
                                   _NotifyingPolicy(self))
        hdr = P.video.hdr
        hdr_vf = None
        if hdr:
            hdr_vf, why = ff.hdr_chain(info, self.width, self.height)
            if hdr_vf is None:
                self._decision(FallbackDecision(
                    "hdr", "HDR10 (PQ) export", "SDR (BT.709)", why, "The video is exported in SDR.",
                    requires_confirmation=True))
                hdr = False
        two_pass = P.video.two_pass
        if two_pass and not hdr and choice.family != "software":
            self._decision(FallbackDecision(
                "two_pass", "two-pass encoding", f"single-pass {choice.label}",
                "two-pass bitrate control is only implemented for the software encoders",
                "Quality-targeted single pass is used instead.", requires_confirmation=True))
            two_pass = False
        if self.output.exists() and not self.overwrite:
            produced = self._produced_by_this_job()
            if not produced:
                raise UsageError(f"Output file already exists: {self.output}",
                                 hint="Choose another name or allow overwriting explicitly.")
        self.output.parent.mkdir(parents=True, exist_ok=True)
        bpp = ff.BITS_PER_PIXEL[P.video.quality]
        est = bpp * self.width * self.height * self.fps * P.video.duration_s / 8
        need = est * (4.5 if (two_pass or hdr) else 2.2) + 64 * 2 ** 20
        for where in {self.output.parent, self.job_dir}:
            free = shutil.disk_usage(where).free
            if free < need:
                raise InsufficientStorageError(
                    f"Not enough free space in {where}: about {need / 2 ** 20:,.0f} MB needed, "
                    f"{free / 2 ** 20:,.0f} MB free.")
        return {"encoder": choice.name, "family": choice.family, "codec": choice.codec,
                "hdr": hdr, "hdr_vf": hdr_vf, "two_pass": two_pass, "ffmpeg": info.ffmpeg, "ffmpeg_version": info.version,
                "estimated_output_mb": round(est / 2 ** 20, 1)}

    def _produced_by_this_job(self) -> bool:
        rec = self.job_dir / "nodes" / "encode.json"
        return rec.is_file()

    def _n_audio(self, ctx) -> Optional[dict]:
        a = self.project.audio
        if not a.enabled:
            return None
        info = ff.probe(self.ffmpeg_path, verify_hw=False)
        res = analyze_file(info, Path(a.path))
        ctx.progress(1.0, f"{len(res.onsets_s)} onsets")
        d = res.to_dict()
        d["path"] = a.path
        return d

    def _n_import(self, ctx) -> dict:
        P = self.project
        tl = load_timeline(P.timeline.path, checkpoint_dir=self.job_dir / "import_ckpt",
                           progress=lambda f, m: ctx.progress(f, m), cancel=self.cancel)
        if P.timeline.sha256 and tl.source_sha256 != P.timeline.sha256:
            raise ProjectError("The Timeline file has changed since this project was saved.",
                               hint="Re-select the Timeline in the project to confirm the new file.")
        tmp = self.job_dir / "timeline.tmp.npz"
        np.savez(tmp, **tl.semantic.to_npz_dict("s"), **tl.raw.to_npz_dict("r"))
        os.replace(tmp, self.job_dir / "timeline.npz")
        return {"sha256": tl.source_sha256, "format": tl.diagnostics.detected_format,
                "semantic_points": len(tl.semantic), "raw_points": len(tl.raw)}

    def _timeline(self) -> Timeline:
        from ..timeline.model import ImportDiagnostics
        with np.load(self.job_dir / "timeline.npz") as d:
            return Timeline(PointColumns.from_npz(d, "s"), PointColumns.from_npz(d, "r"), ImportDiagnostics())

    def _n_normalize(self, ctx) -> dict:
        cols, stats = select_route_points(self._timeline(), journey_config(self.project))
        _save_cols(self.job_dir / "normalized.npz", cols)
        return {"points": len(cols), "stats": stats}

    def _n_filter(self, ctx) -> dict:
        cols, removed = filter_route_points(_load_cols(self.job_dir / "normalized.npz"),
                                            journey_config(self.project))
        _save_cols(self.job_dir / "filtered.npz", cols)
        return {"points": len(cols), "outliers_removed": removed}

    def _journey(self) -> Journey:
        return journey_from_columns(_load_cols(self.job_dir / "filtered.npz"),
                                    self.project.journey.trip_detection,
                                    self.graph.states["filter"].result["outliers_removed"])

    def _n_journey(self, ctx) -> dict:
        j = self._journey()
        return {k: v for k, v in j.stats.items() if k != "fusion"}

    def _n_camera(self, ctx) -> dict:
        j = self._journey()
        cfg = camera_config(self.project)
        audio = ctx.result("analyze_audio")
        synced = None
        if audio and self.project.audio.beat_sync and audio.get("onsets_s"):
            default_start = self.project.video.duration_s - cfg.outro_transition_s - cfg.outro_hold_s
            o = nearest_onset(audio["onsets_s"], default_start)
            if o is not None:
                cfg.outro_start_frame = int(round(o * self.fps))
                synced = o
        plan = plan_frames(j, cfg, width=self.width, height=self.height, fps=self.fps,
                           duration_s=self.project.video.duration_s)
        tmp = self.job_dir / "camera.tmp.npz"
        plan.save(tmp)
        os.replace(tmp, self.job_dir / "camera.npz")
        rep = jerk_report(plan)
        return {"frames": plan.frame_count, "jerk": {k: round(v, 6) for k, v in rep.items()},
                "outro_synced_to_onset_s": synced}

    def _plan(self) -> FramePlan:
        return FramePlan.load(self.job_dir / "camera.npz")

    def _n_tiles(self, ctx) -> dict:
        P = self.project
        provider = make_provider(provider_spec(P))
        if isinstance(provider, PlainProvider):
            return {"provider": "plain", "requested": 0, "missing": 0}
        plan = self._plan()
        need = tiles_for_plan(plan.cx, plan.cy, plan.span_y, plan.aspect, plan.width,
                              provider.max_zoom, provider.tile_size)
        cache = TileCache(provider, self.tile_root or cache_dir() / "tiles", offline=P.visual.offline)
        rep = cache.prefetch(need, progress=lambda d, t: ctx.progress(d / max(1, t), f"tiles {d}/{t}"),
                             cancel=self.cancel)
        self.cancel.raise_if_cancelled()
        allow = False
        if rep.missing:
            reasons = sorted(set(rep.failed.values()))[:3]
            d = FallbackDecision(
                "map_tiles", f"{rep.requested} map tiles from {provider.name}",
                f"background-coloured placeholders for {rep.missing} missing tiles",
                f"{rep.missing} tiles could not be obtained ({'; '.join(reasons)})",
                "Parts of the map will be blank in the video.", requires_confirmation=True)
            if self.allow_placeholder is True:
                self.policy.applied.append(d)
                self._emit({"event": "fallback", "decision": d.to_dict()})
            else:
                self._decision(d)
            allow = True
        return {"provider": provider.id, "requested": rep.requested, "from_cache": rep.from_cache,
                "downloaded": rep.downloaded, "missing": rep.missing, "allow_placeholder": allow}

    def _graded_dir(self) -> Path:
        return graded_cache_dir(self.project, self.tile_root)

    def _n_cache(self, ctx) -> dict:
        P = self.project
        provider = make_provider(provider_spec(P))
        if isinstance(provider, PlainProvider):
            return {"graded": 0}
        plan = self._plan()
        need = sorted(tiles_for_plan(plan.cx, plan.cy, plan.span_y, plan.aspect, plan.width,
                                     provider.max_zoom, provider.tile_size))
        cache = TileCache(provider, self.tile_root or cache_dir() / "tiles", offline=True)
        comp = MapCompositor(cache, get_theme(P.visual.theme), allow_placeholder=True,
                             graded_dir=self._graded_dir())
        n = 0
        for i, key in enumerate(need):
            self.cancel.raise_if_cancelled()
            if comp.graded_array(key) is not None:
                n += 1
            if i % 32 == 0:
                ctx.progress(i / max(1, len(need)), f"grading {i}/{len(need)}")
        return {"graded": n, "tiles": len(need)}

    # ---------------------------------------------------------- segments
    def _seg_dir(self) -> Path:
        d = self.job_dir / "segments"
        d.mkdir(exist_ok=True)
        return d

    def _seg_manifest(self) -> dict:
        p = self._seg_dir() / "manifest.json"
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        return {"segments": {}}

    def _seg_manifest_save(self, m: dict) -> None:
        p = self._seg_dir() / "manifest.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, indent=1), encoding="utf-8")
        os.replace(tmp, p)

    def _segments_valid(self, result) -> bool:
        if not result:
            return False
        m = self._seg_manifest()
        for s in result.get("segments", []):
            rec = m["segments"].get(str(s))
            if not rec or not (self._seg_dir() / rec["file"]).is_file():
                return False
        return True

    def _n_render(self, ctx) -> dict:
        P = self.project
        pre = ctx.result("preflight")
        info = ff.probe(self.ffmpeg_path)
        choice = ff.EncoderChoice(pre["codec"], pre["family"], pre["encoder"])
        mezz = bool(pre["two_pass"] or pre["hdr"])
        codec_args = ff.encoder_args(choice, P.video.quality, self.fps, mezzanine=mezz)
        j = self._journey()
        plan = self._plan()
        tiles = ctx.result("prepare_tiles") or {}
        renderer, comp, _ = build_renderer(P, j, plan, allow_placeholder=bool(tiles.get("allow_placeholder")),
                                           graded_dir=self._graded_dir(), offline=True,
                                           tile_root=self.tile_root)
        seg_frames = max(1, int(round(self.segment_seconds * self.fps)))
        n_seg = (self.total_frames + seg_frames - 1) // seg_frames
        manifest = self._seg_manifest()
        manifest["config"] = {"encoder": choice.name, "mezzanine": mezz, "segment_frames": seg_frames}
        if manifest.get("config_fp") not in (None, fingerprint_of(manifest["config"])):
            manifest["segments"] = {}
        manifest["config_fp"] = fingerprint_of(manifest["config"])
        done_before = 0
        for s in range(n_seg):
            rec = manifest["segments"].get(str(s))
            if rec and (self._seg_dir() / rec["file"]).is_file():
                done_before += rec["frames"]
        self.eta.skip(done_before)
        if done_before:
            self._emit({"event": "resume", "frames_restored": done_before})
        log_path = self.job_dir / "ffmpeg.log"
        for s in range(n_seg):
            rec = manifest["segments"].get(str(s))
            if rec and (self._seg_dir() / rec["file"]).is_file():
                continue
            self.cancel.raise_if_cancelled()
            f0 = s * seg_frames
            f1 = min(self.total_frames, f0 + seg_frames)
            name = f"seg_{s:05d}.mkv"
            part = self._seg_dir() / (name + ".part")
            enc = ff.FrameEncoder(info, part, self.width, self.height, self.fps, codec_args, log_path)
            q: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=6)
            err: List[BaseException] = []

            def writer():
                try:
                    while True:
                        item = q.get()
                        if item is None:
                            return
                        enc.write(item)
                except BaseException as e:  # noqa: BLE001
                    err.append(e)
                    while True:          # drain so the producer never blocks
                        if q.get() is None:
                            return

            wt = threading.Thread(target=writer, daemon=True, name="tlx-writer")
            wt.start()
            try:
                for f in range(f0, f1):
                    if self.cancel.cancelled or err:
                        break
                    t0 = time.perf_counter()
                    img = renderer.render(f)
                    q.put(bytes(img.constBits()))
                    self.eta.frame_done(time.perf_counter() - t0)
                    if f % 4 == 0 or f == f1 - 1:
                        ctx.progress(self.eta.done / self.total_frames,
                                     f"frame {self.eta.done}/{self.total_frames}")
            finally:
                q.put(None)
                wt.join()
            if self.cancel.cancelled:
                enc.abort()
                part.unlink(missing_ok=True)
                raise RenderCancelledError(self.cancel.reason or "cancelled")
            if err:
                enc.abort()
                part.unlink(missing_ok=True)
                raise err[0]
            enc.close()
            os.replace(part, self._seg_dir() / name)
            manifest["segments"][str(s)] = {"file": name, "frames": f1 - f0, "first": f0}
            self._seg_manifest_save(manifest)
            self._emit({"event": "segment_committed", "segment": s, "segments": n_seg})
        if comp.placeholders_drawn:
            self._emit({"event": "warning", "message": f"{comp.placeholders_drawn} placeholder tiles drawn"})
        return {"segments": list(range(n_seg)), "segment_frames": seg_frames, "frames": self.total_frames,
                "mezzanine": mezz, "encoder": choice.name}

    # --------------------------------------------------------------- encode
    def _n_encode(self, ctx) -> dict:
        P = self.project
        pre = ctx.result("preflight")
        info = ff.probe(self.ffmpeg_path)
        man = self._seg_manifest()
        segs = [man["segments"][str(s)]["file"] for s in ctx.result("render_frames")["segments"]]
        lst = self._seg_dir() / "concat.txt"
        def _q(path: Path) -> str:      # concat-demuxer quoting: ' → '\''
            return "'" + path.as_posix().replace("'", "'\\''") + "'"
        lst.write_text("".join(f"file {_q(self._seg_dir() / s)}\n" for s in segs), encoding="utf-8")
        tmp_out = self.output.with_name(self.output.stem + ".nrpart.mp4")
        dur = self.total_frames / self.fps
        audio = ctx.result("analyze_audio")
        inputs = ["-f", "concat", "-safe", "0", "-i", str(lst)]
        amap: List[str] = []
        if audio:
            inputs += ["-i", audio["path"]]
            ducks = []
            if P.audio.ducking:
                cfg = camera_config(P)
                ducks.append((0.0, max(1.5, cfg.intro_s + 1.5)))
                if P.title.ending_title:
                    ducks.append((max(0.0, dur - cfg.outro_hold_s - 0.6), dur))
            af = audio_filter(dur, P.audio.volume, P.audio.fade_out_s, ducks, P.audio.duck_db)
            amap = ["-map", "0:v:0", "-map", "1:a:0", "-af", af, "-c:a", "aac", "-b:a", "192k"]
        else:
            amap = ["-map", "0:v:0"]
        n_in = inputs.count("-i")
        inputs += ff.write_metadata_file(
            self.job_dir / "metadata.txt", title=build_overlays(self._journey(), P.title).title,
            comment=f"Rendered by TimelinerX {__version__} ({RENDER_ENGINE_VERSION})")
        meta = ["-map_metadata", str(n_in), "-movflags", "+faststart", "-t", f"{dur:.3f}"]
        choice = ff.EncoderChoice(pre["codec"], pre["family"], pre["encoder"])
        prog = lambda f: ctx.progress(f, "encoding")  # noqa: E731
        log = self.job_dir / "ffmpeg.log"
        hdr_done = False
        hdr_out = bool(pre["hdr"])
        if pre["hdr"]:
            crf = ff.QUALITY_TABLE[P.video.quality][0] + 2
            last_err = None
            # the preflight-verified chain first; the others only if this build still rejects the
            # real frames (never a silent change of result: all candidates produce HDR10)
            for vf in ff.hdr_chain_candidates(pre.get("hdr_vf")):
                venc = ["-vf", vf, "-c:v", "libx265", "-crf", str(crf), "-preset",
                        ff.QUALITY_TABLE[P.video.quality][1], "-tag:v", "hvc1",
                        "-x265-params", ff.HDR_X265,
                        "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
                        "-g", str(self.fps * 2)]
                try:
                    ff.run_ffmpeg(info, inputs + amap + venc + meta + [str(tmp_out)], self.cancel, log, prog,
                                  dur)
                    hdr_done = True
                    break
                except RenderFailedError as e:
                    last_err = e
                    tmp_out.unlink(missing_ok=True)
            if not hdr_done:
                self._decision(FallbackDecision(
                    "hdr", "HDR10 (PQ) export", "SDR (BT.709)",
                    f"FFmpeg could not convert the frames to HDR10 ({str(last_err)[-200:]})",
                    "The video is exported in SDR.", requires_confirmation=True))
                hdr_out = False
                sdr = ["-c:v", "libx265", "-crf", str(crf), "-preset", ff.QUALITY_TABLE[P.video.quality][1],
                       "-pix_fmt", "yuv420p", "-tag:v", "hvc1", "-x265-params", "log-level=error",
                       "-g", str(self.fps * 2)]
                ff.run_ffmpeg(info, inputs + amap + sdr + meta + [str(tmp_out)], self.cancel, log, prog, dur)
                hdr_done = True
        if hdr_done:
            pass
        elif pre["two_pass"]:
            bpp = ff.BITS_PER_PIXEL[P.video.quality]
            kbps = int(bpp * self.width * self.height * self.fps / 1000)
            enc_name = "libx265" if pre["codec"] == "hevc" else "libx264"
            passlog = str(self.job_dir / "2pass")
            base = ["-c:v", enc_name, "-b:v", f"{kbps}k", "-preset", ff.QUALITY_TABLE[P.video.quality][1],
                    "-pix_fmt", "yuv420p", "-g", str(self.fps * 2)]
            if enc_name == "libx265":
                p1 = base + ["-x265-params", "pass=1:log-level=error", "-tag:v", "hvc1"]
                p2 = base + ["-x265-params", "pass=2:log-level=error", "-tag:v", "hvc1"]
            else:
                p1 = base + ["-pass", "1"]
                p2 = base + ["-pass", "2"]
            ff.run_ffmpeg(info, inputs + ["-map", "0:v:0"] + p1 + ["-passlogfile", passlog, "-an", "-f", "null",
                                                                     os.devnull], self.cancel, log,
                          lambda f: ctx.progress(f * 0.5, "two-pass: analysis"), dur)
            ff.run_ffmpeg(info, inputs + amap + p2 + ["-passlogfile", passlog] + meta + [str(tmp_out)],
                          self.cancel, log, lambda f: ctx.progress(0.5 + f * 0.5, "two-pass: encode"), dur)
        else:
            ff.run_ffmpeg(info, inputs + amap + ["-c:v", "copy"] + meta + [str(tmp_out)], self.cancel, log,
                          prog, dur)
        os.replace(tmp_out, self.output)
        return {"output": str(self.output), "bytes": self.output.stat().st_size,
                "encoder": choice.name, "hdr": hdr_out, "two_pass": pre["two_pass"],
                "audio": bool(audio)}

    # --------------------------------------------------------------- verify
    def _n_verify(self, ctx) -> dict:
        info = ff.probe(self.ffmpeg_path)
        pj = ff.ffprobe_json(info, self.output)
        vs = [s for s in pj.get("streams", []) if s.get("codec_type") == "video"]
        if not vs:
            raise VerificationFailedError("The output contains no video stream.")
        v = vs[0]
        problems = []
        if int(v.get("width", 0)) != self.width or int(v.get("height", 0)) != self.height:
            problems.append(f"size {v.get('width')}×{v.get('height')} ≠ {self.width}×{self.height}")
        num, _, den = str(v.get("r_frame_rate", "0/1")).partition("/")
        fps = float(num) / float(den or 1)
        if abs(fps - self.fps) > 0.01:
            problems.append(f"frame rate {fps:.3f} ≠ {self.fps}")
        frames = int(v.get("nb_read_packets") or v.get("nb_frames") or 0)
        if frames != self.total_frames:
            problems.append(f"{frames} frames ≠ expected {self.total_frames}")
        if self.project.audio.enabled and not any(s.get("codec_type") == "audio" for s in pj["streams"]):
            problems.append("audio track missing")
        # decode test on the first and last two seconds
        dur = self.total_frames / self.fps
        for start in (0.0, max(0.0, dur - 2.0)):
            r = ff.run([info.ffmpeg, "-v", "error", "-nostdin", "-ss", f"{start:.3f}", "-t", "2",
                        "-i", str(self.output), "-f", "null", "-"], timeout=300)
            if r.returncode != 0 or r.stderr.strip():
                problems.append(f"decode errors near {start:.1f}s: {r.stderr.decode(errors='replace')[:200]}")
        if problems:
            raise VerificationFailedError("Output verification failed: " + "; ".join(problems),
                                          hint="The file was not added to the library.")
        return {"width": self.width, "height": self.height, "fps": self.fps, "frames": frames,
                "codec": v.get("codec_name"), "duration_s": float(pj["format"].get("duration", dur)),
                "size_bytes": int(pj["format"].get("size", 0)),
                "color_transfer": v.get("color_transfer")}

    def _n_meta(self, ctx) -> dict:
        P = self.project
        g = self.graph
        sidecar = Path(str(self.output) + ".nrmeta.json")
        meta = {"app": "TimelinerX", "app_version": __version__,
                "render_engine_version": RENDER_ENGINE_VERSION,
                "rendered_at": datetime.now(timezone.utc).isoformat(),
                "video": g.states["verify"].result, "project": P.to_dict(),
                "journey": g.states["build_journey"].result,
                "camera": g.states["plan_camera"].result, "tiles": g.states["prepare_tiles"].result,
                "encoder": g.states["encode"].result,
                "fallbacks_applied": [d.to_dict() for d in self.policy.applied],
                "node_timings_s": {n: g.states[n].duration for n in g.order}}
        # the sidecar never contains coordinates: drop keyframe positions
        for kf in meta["project"]["camera"]["keyframes"]:
            kf.pop("lat", None)
            kf.pop("lon", None)
        sidecar.write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
        return {"sidecar": str(sidecar)}

    def _n_thumb(self, ctx) -> dict:
        info = ff.probe(self.ffmpeg_path)
        dur = self.total_frames / self.fps
        t = max(0.0, dur - max(0.3, self.project.camera.outro_hold_s / 2))
        thumb = Path(str(self.output) + ".thumb.jpg")
        r = ff.run([info.ffmpeg, "-y", "-v", "error", "-nostdin", "-ss", f"{t:.3f}", "-i", str(self.output),
                    "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "3", str(thumb)], timeout=120)
        if r.returncode != 0 or not thumb.is_file():
            raise RenderFailedError("Thumbnail extraction failed: " + r.stderr.decode(errors="replace")[:300])
        return {"thumbnail": str(thumb)}

    def _n_library(self, ctx) -> Optional[int]:
        if not self.add_to_lib:
            return None
        lib = self.library or Library()
        v = self.graph.states["verify"].result
        js = self.graph.states["build_journey"].result
        enc = self.graph.states["encode"].result
        P = self.project
        e = VideoEntry(title=build_overlays(self._journey(), P.title).title or P.name, path=str(self.output),
                       thumbnail=ctx.result("generate_thumbnail")["thumbnail"],
                       date_start=(js.get("first") or "")[:10], date_end=(js.get("last") or "")[:10],
                       duration_s=v["duration_s"], width=v["width"], height=v["height"], fps=v["fps"],
                       size_bytes=v["size_bytes"], codec=v["codec"], encoder=enc["encoder"],
                       theme=P.visual.theme, camera_mode=P.camera.mode,
                       render_engine_version=RENDER_ENGINE_VERSION,
                       extra=json.dumps({"hdr": enc["hdr"], "two_pass": enc["two_pass"]}))
        return lib.add(e)


class _NotifyingPolicy(FallbackPolicy):
    """Wraps the job policy so informational encoder decisions are also emitted as events."""

    def __init__(self, job: RenderJob):
        super().__init__()
        self.job = job

    def resolve(self, decision: FallbackDecision) -> FallbackDecision:
        return self.job._decision(decision)


def default_output_path(project: Project, out_dir: Optional[str] = None) -> Path:
    base = Path(out_dir or project.output_dir or (Path.home() / "Videos" / "TimelinerX"))
    name = safe_filename(project.name or "journey")
    w, h = project.video.dimensions()
    return base / f"{name} {w}x{h} {project.video.fps}fps.mp4"
