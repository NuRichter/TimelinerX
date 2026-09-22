# Architecture

## Layers

```
UI (ui/, PySide6)  ──┐
CLI (cli/)         ──┼──> pipeline/ (RenderJob DAG, preview, watch, compare)
                     │        │
                     │        ├── timeline/  → journeys/ → camera/  (pure Python/numpy, no Qt)
                     │        ├── maps/ + rendering/               (Qt raster painting on QImage)
                     │        └── encoding/                        (FFmpeg subprocesses)
                     └──> projects/, storage/, environment/, plugins/, i18n/
```

The UI contains no business logic: every page edits the `Project` model and calls the same
functions the CLI uses. Rendering never happens on the UI thread.

## Axioms and where they are enforced

| Axiom | Mechanism |
|---|---|
| Local-first | No network except tile requests to the selected provider and an optional, user-configured update manifest. |
| Reliability | Import errors are typed (`core/errors.py`) and carry hints; renders are checkpointed per node and per segment; output is verified before it reaches the library; cancellation kills FFmpeg children (process registry, Windows Job Object, Linux `PR_SET_PDEATHSIG`). |
| No silent fallback | `core/fallback.py`: any degradation (encoder, HDR→SDR, two-pass, placeholder tiles) is a `FallbackDecision` resolved by a `FallbackPolicy`; explicit requests require consent; every applied decision is emitted as an event and stored in the video's metadata. |
| Determinism | Fixed grain seed, bundled fonts, pure functions of (inputs, frame index); `render_engine_version` recorded in projects and videos; perceptual-hash regression test and a pixel-identity test. |

## Render DAG

```
preflight ─────────────────────────────────────────────┐
analyze_audio ───────────────────────┐                  │
import → normalize → filter → build_journey → plan_camera → prepare_tiles → prepare_cache → render_frames → encode → verify ─┬─ finalize_metadata ─┐
                                                                                                                             └─ generate_thumbnail ─┴─ add_to_library
```

* Checkpoint record per node: `jobs/<id>/nodes/<node>.json` with a fingerprint of the node's inputs and a
  digest of its dependencies' results. A node is reused when both match and its `validate` callback
  confirms its artefacts exist. Cheap nodes (preflight, verify, …) are not checkpointed and simply re-run.
* `render_frames` encodes fixed-length segments (default 4 s) to `segments/seg_NNNNN.mkv` via a `.part`
  file and an atomic rename, recording each in `segments/manifest.json`. On resume only missing segments
  are rendered.
* `encode` concatenates segments with stream copy (or re-encodes for two-pass/HDR) into
  `<name>.nrpart.mp4`, then renames it to the final name. Killing the process during `encode` therefore
  never leaves a partial file under the final name and never forces re-rendering.

## Camera

1. Framing layer (upstream-derived): per-sample target viewport from trip legs and context windows,
   dead-zone follower, visual-work pacing.
2. Cinema layer: the camera is expressed as *marker position + offset in viewport units* and the offset
   and log-scale are low-passed with a zero-phase Gaussian (no lag, no world-space drag during
   large zoom changes); Catmull-Rom sampling; anticipation; rule-of-thirds correction of the along-track
   offset; van Wijk–Nuij zoom-pan interpolation for intro, outro and keyframes; per-frame low-pass;
   marker visibility guard (skipped while a manual keyframe is active).
3. Verification: `jerk_report` measures the maximum frame-to-frame acceleration of pan (viewport units)
   and zoom (log scale).

## Rendering

`MapCompositor` draws two tile zoom levels with a smoothstep cross-fade. Tiles are graded by the theme's
node graph once and cached on disk (`cache/graded/<theme+provider hash>/`). `FrameRenderer` draws the
trail (great-circle densified, screen-space decimated, gradient via a Source-composited layer), bloom
(alpha-only blur at 1/5 resolution, screen-composited), marker, titles and attribution, then vignette and
grain through Qt compositing. Frames are BGRA and piped to FFmpeg, which converts to BT.709 limited range.

## Data locations

`platformdirs` per-user folders (or `TIMELINERX_HOME`): `data/` (library.sqlite3, jobs/, logs/, plugins/),
`cache/` (tiles/, graded/, import/, ui/), `config/settings.json`.
