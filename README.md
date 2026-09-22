# TimelinerX

TimelinerX is a Windows desktop application (also runs on Linux and
macOS from source) that imports a Google Timeline export and renders the
travel history as an animated map video (MP4). Processing is local: the
Timeline file is never uploaded, there is no account and no telemetry.

It builds on the algorithms of [Google Timeline Visualizer](https://github.com/mahlernim/google-timeline-visualizer)
by mahlernim (MIT), re-implemented for the desktop and extended; see
[Attribution](#attribution).

* Version 1.0.0 · render engine `tlx-2.0.0` · Made with NuRichter Workspace
* Licence: MIT (see `LICENSE` and `THIRD_PARTY_NOTICES.md`)

---

## Contents

1. [What it does](#what-it-does)
2. [Features](#features)
3. [System requirements](#system-requirements)
4. [Installation](#installation)
5. [Building from source](#building-from-source)
6. [FFmpeg](#ffmpeg)
7. [Map providers](#map-providers)
8. [Privacy model](#privacy-model)
9. [Supported Timeline formats](#supported-timeline-formats)
10. [Import diagnosis and repair](#import-diagnosis-and-repair)
11. [Rendering guide](#rendering-guide)
12. [High-resolution renders](#high-resolution-renders)
13. [Command line](#command-line)
14. [Troubleshooting](#troubleshooting)
15. [Architecture](#architecture)
16. [Testing](#testing)
17. [Licence, third-party notices and attribution](#licence-third-party-notices-and-attribution)
18. [Known limitations](#known-limitations)

---

## What it does

1. Import `Timeline.json` (Android on-device export, iOS export) or a Google Takeout `.zip`
   (including legacy `Records.json` and *Semantic Location History*).
2. Diagnose problems; optionally write a repaired copy (the original is never modified).
3. Choose a period, route source and outlier filtering.
4. Choose a visual theme, camera mode, titles, resolution, frame rate and encoder.
5. Preview with the same renderer that produces the export.
6. Render. The render is checkpointed: if the app, the computer or the power
   fails, it resumes where it stopped.
7. The output is verified (dimensions, frame rate, frame count, decode test)
   before it is added to the Video Library.

## Features

**Import**
* Streaming parser for files ≥ 50 MB with resumable checkpoints every ~16 MB.
* ZIP intake with decompression-bomb, entry-count and path-traversal guards.
* Detailed diagnostics: detected format, counts, skipped records by reason,
  duplicates, reversed export order, timezone handling.

**Repair** — ten passes producing HIGH / MEDIUM / LOW confidence actions (see below),
with HTML/JSON/text reports.

**Journey** — period selection on the local calendar, semantic or detailed-first
(raw signal) routes, conservative teleport filter, trip detection.

**Journey Builder** — Zoom style (Fixed, Balanced, Active, Close-Up), Long-trip
detection (Conservative, Balanced, Sensitive), Local trip framing (Off, Balanced,
Close) and Long-trip pacing (Natural, Balanced, Faster, Fastest), with a live route
sketch and calm/lively duration estimates. Transport modes from the export are kept;
flights fly as arcs with a plane marker.

**Camera** — four zoom styles; vertex-accurate visual pacing plus screen-speed
equalisation, optional cinematic motion blur; zero-phase
smoothing in viewport space, C1 spline sampling, anticipation, rule-of-thirds
lead room or centred framing, eased intro/outro using optimal zoom-pan
interpolation, Director's-Cut keyframes. Camera smoothness is measured, not
assumed (see [Testing](#testing)).

**Look** — ten themes (Light, Dark, Neon Dark Blue/Red/Yellow/Green/Purple,
Neon Cyan, Monochrome, High Contrast) plus an experimental slot, all defined
as JSON grading node graphs; gradient trail with selective bloom, soft
shadow, pulse marker, vignette, deterministic film grain; title layouts
(corner, centred, minimal, lower third, ending card) inside a 5 % title-safe area.

**Output** — 480p, 720p, 1080p, 1440p, 2160p, 4K (4096 px long edge), 8K
(7680 px long edge); 16:9, 9:16, 1:1 or custom even dimensions; 24/30/60 fps;
H.264/HEVC; NVENC → Quick Sync → AMF → software selection with explicit
fallback notices; optional two-pass (software encoders); experimental HDR10 export.

**Other** — own audio track with local beat detection and ducking; render
queue with ETA, render speed, CPU and memory; render-graph inspector; video
library; headless CLI; watch folder; split-screen / sequential comparison;
plugin SDK for themes and map providers; UI in English, Bahasa Indonesia, Español, Français, Deutsch, Português (Brasil), Русский, العربية (RTL), 中文 and 日本語;
light and dark appearance; keyboard navigation.

## System requirements

| | Minimum | Recommended |
|---|---|---|
| OS | Windows 10 64-bit (1809+) | Windows 11 64-bit |
| CPU | 2 cores | 6+ cores |
| RAM | 8 GB | 16 GB (32 GB for 2160p and above) |
| GPU | not required | NVIDIA/Intel/AMD GPU with a hardware H.264/HEVC encoder |
| Disk | 2 GB free + ~2× the expected video size | SSD |
| Other | FFmpeg 5.1+ with ffprobe | FFmpeg 6/7 with libx264, libx265 and zscale |

The in-app environment scan reports what it detected and derives a
Comfortable / Heavy / Extreme recommendation. The scoring rules are shown in
the app; they are estimates, not performance guarantees.

## Installation

**Windows executable.** Run `TimelinerX.exe`. No Python installation
is needed. Install FFmpeg (see [FFmpeg](#ffmpeg)) unless your build bundles it.
Unsigned builds trigger a SmartScreen prompt on first launch.

**From source (any OS).**

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m timelinerx   # GUI (run from the repository with PYTHONPATH=src, or pip install -e .)
```

or `pip install -e .` and use the `timelinerx-gui` / `timelinerx` commands.

## Building from source

Tested toolchain: Python 3.11 (3.12 and 3.13 also supported; the build script picks one automatically), the versions pinned in `requirements*.txt`,
PyInstaller 6.22.3.

```powershell
# Windows, from the repository root
build_windows.bat                      # venv, pinned deps, tests, PyInstaller
build_windows.bat -SkipTests -OneDir   # folder build (recommended for LGPL re-linking of Qt)
build_windows.bat -FFmpegDir C:\ffmpeg\bin -SignCert cert.pfx -SignPassword ****
```

Outputs:

* `dist\TimelinerX.exe` — the desktop application
* `dist\timelinerx-cli.exe` — console build of the same program for scripts (JSON-lines output, exit codes)
* `*.sha256` checksums

Code signing is optional (`-SignCert`); it needs `signtool.exe` from the
Windows SDK. The CI workflow `.github/workflows/windows-build.yml` runs the
tests and builds both executables on `windows-latest`.

`python scripts/clean_build.py` removes build artefacts.

Environment variables: `TIMELINERX_HOME` (put data, cache and config in one
folder — portable installs, tests), `TIMELINERX_FFMPEG_DIR` (FFmpeg folder),
`CARTO_BASEMAP_API_KEY` (optional CARTO key), `QT_QPA_PLATFORM=offscreen`
(headless tests).

## FFmpeg

TimelinerX runs `ffmpeg` and `ffprobe` as separate processes. Lookup order:
path set in *Settings → Rendering*, `TIMELINERX_FFMPEG_DIR`, an `ffmpeg` folder
next to the executable, then `PATH`.

At startup and before every render, FFmpeg is probed: version, encoders,
filters, and a 0.5-second **test encode for each hardware encoder** — an
encoder listed by FFmpeg may still fail without a GPU or driver. A render
never starts if FFmpeg, ffprobe or a usable encoder is missing.

Encoder selection: NVENC → Quick Sync → AMF → libx264/libx265. With
*Automatic*, the chosen encoder and the reason for skipping the others are
shown in the queue and stored in the video metadata. If you explicitly pick
an encoder that is unavailable, the render stops and asks before using a
different one. H.264 hardware encoders are limited to 4096 px per side; 8K
therefore needs HEVC or libx264.

If you redistribute a build with FFmpeg bundled, read the FFmpeg section of
`THIRD_PARTY_NOTICES.md` first.

## Map providers

**CARTO needs an API key.** Since 2026 CARTO stamps "API KEY REQUIRED" on every tile requested
without one. Get a free key (personal, research, non-profit use; 5 million tiles/month) at
<https://carto.com/basemaps/apikey/> and paste it into *Settings → Maps → CARTO API key*
(*Verify* checks it online). Other sources, in order: `--carto-key` (CLI),
`TIMELINERX_CARTO_KEY` / `CARTO_BASEMAP_API_KEY`, or a key built into the executable
(`build_windows.bat -CartoKey …`, or the first line of an untracked `carto_key.txt`). A built-in
key can be extracted from the `.exe` by anyone who has it and is shared by all its users — for a
public release prefer asking users to enter their own key. Without a key, TimelinerX refuses to
render CARTO maps rather than produce a watermarked video.

| Provider | Network | Notes |
|---|---|---|
| CARTO (default; light or dark style follows the theme) | yes | © OpenStreetMap contributors © CARTO, rendered in every frame. Rate-limited, cached on disk. |
| CARTO Voyager | yes | as above |
| MBTiles file | no | your own raster `.mbtiles`; its attribution metadata is rendered |
| Plain | no | no tiles: themed background with a graticule |
| Plugins | depends | see `docs/PLUGINS.md` |

Before rendering, every tile the camera will need is prefetched. If any tile
is unavailable, the render asks whether to continue with placeholder tiles —
it never substitutes them silently. *Offline mode* uses the cache only.
Dark themes (Dark, Neon, High Contrast) are designed for dark basemaps; with a
light-only provider such as Voyager or a light MBTiles file, prefer Light or
Monochrome.

## Privacy model

* The Timeline file is read locally and never modified, uploaded or copied
  into projects (projects store its path and SHA-256).
* No account, no telemetry, no automatic update downloads.
* Map tile requests reveal which map areas a video shows (roughly where the
  route goes) to the tile server. Use Plain, MBTiles or offline mode to avoid
  any network request.
* Log files never contain coordinates: number pairs that look like coordinates
  are replaced with `<coord>` unless you enable *Settings → Privacy → Write raw
  coordinates to local debug logs*.
* The video library stores metadata only. The `.nrmeta.json` sidecar next to
  each video stores the settings used, without keyframe coordinates.
* Temporary render files live in the per-user data folder and are deleted
  after a successful render (or kept for resume after an interruption; they
  can be discarded from the Render Queue).
* Repair reports contain the coordinates of the records they changed, because
  they stay on your computer. Do not share them if you consider that private.

## Supported Timeline formats

| Format | Where it comes from | Root |
|---|---|---|
| On-device Timeline (object) | Android: *Settings → Location → Timeline → Export* | `{"semanticSegments": [...], "rawSignals": [...]}` |
| On-device Timeline (array) | iOS Google Maps export | `[ {...segment...}, ... ]` |
| Takeout Records | Google Takeout (before 2024) | `{"locations": [ {latitudeE7, longitudeE7, timestamp or timestampMs}, ... ]}` |
| Takeout Semantic Location History | Google Takeout (before 2024), monthly files | `{"timelineObjects": [ {placeVisit} / {activitySegment} ]}` |
| Takeout `.zip` | Google Takeout | the files above inside the archive |

Coordinates may be `"lat°, lon°"`, `"geo:lat,lon"`, wrapped objects, or E7
integers. Timestamps may carry offsets, be UTC, lack a timezone (then they are
not trusted for ordering across records) or be epoch milliseconds.

## Import diagnosis and repair

If an import fails or reports skipped records, *Diagnose & repair…* runs these
passes on the raw file:

1. Forensic scan (encoding, BOM, NUL bytes, size)
2. Structural recovery (trailing data, trailing commas, salvage of complete records from truncated/corrupted files)
3. Schema normalisation (E7 in strings, epoch-ms timestamps)
4. Coordinate repair (out-of-range, Null Island, swapped latitude/longitude)
5. Timestamp repair (unparseable, implausible dates, end before start)
6. Ordering repair (newest-first exports, out-of-order segments)
7. Duplicate cleanup (byte-identical records)
8. Outlier analysis (teleport excursions; robust MAD z-score of speed)
9. Semantic reconstruction (missing startTime or activity endpoints from paths)
10. Validation (the repaired file is re-imported)
11. Report

Each proposed action is classified:

* **HIGH** — deterministic and provably correct (BOM/UTF-16 decoding, E7→degrees, exact duplicates). Selected by default.
* **MEDIUM** — heuristic or statistical (teleport excursions, reversed order, salvage). Selected by default, can be unticked.
* **LOW** — ambiguous (lat/lon swap without context, end/start swap, a single fast hop that may be a real flight). Never applied unless you tick it.

Outputs, next to the source file: `<name>.fixed.json`, `<name>.repair-report.html`,
`<name>.repair-report.json`, `<name>.repair-log.txt`. The source file is never
overwritten.

## Rendering guide

* **Pacing.** *Visual motion + zoom* (default) spends screen time in
  proportion to how much ground moves across the view and how much the camera
  zooms, so long flights do not dominate and short commutes stay visible.
* **Camera modes.** Fixed (one zoom), Steady (calm overview), Dynamic (zooms in
  on local movement, out for transfers), Close-Up (tight local detail), Active
  (energetic, for short social clips).
* **Composition.** Rule of thirds keeps the marker behind the centre with open
  space in the direction of travel; Centred is the minimal alternative.
* **Director's Cut.** In Preview, *Add keyframe here* stores the current view;
  edit time, span, hold and ramp in *Visual Settings → Camera*. Keyframes take
  precedence over automatic framing and use the same easing.
* **Audio.** Your own MP3/WAV/FLAC file; optional alignment of the ending
  zoom-out to the nearest detected beat (±0.75 s), ducking under titles and a
  fade-out. Off by default.
* **Two-pass** (software encoders): longer render, more consistent bitrate.
* **HDR10 (experimental).** The render is SDR; this option places it in an
  HEVC HDR10 container (BT.2020, PQ) with SDR white at 203 nits. It needs
  libx265 and the zscale filter; if missing you are asked before falling back to SDR.
* **Stop vs. Cancel.** *Stop (keep progress)* leaves checkpoints so the render
  appears under *Interrupted renders*; *Cancel and discard* deletes them.

## High-resolution renders

For outputs of 1440p and above (or ≥ 3840 px wide) the Video Settings page
shows a warning based on the environment scan: RAM, VRAM, verified encoders
and free disk space, with an estimate of the temporary disk space needed.
It never blocks the render and never downscales automatically.
Measured throughput on a 2-core machine without a GPU is in `docs/BENCHMARKS.md`.

## Command line

The CLI uses the same pipeline as the GUI. From source: `python -m timelinerx <command>`;
Windows build: `timelinerx-cli.exe <command>`.

```text
new-project  --timeline T.json --out p.nrproj [--name N --start YYYY-MM-DD --end YYYY-MM-DD --resolution 1080p ...]
render       --project p.nrproj [--output out.mp4 --resolution 2160p --fps 60 --encoder nvenc --two-pass --hdr
             --accept-fallback --allow-placeholder-tiles --overwrite --json]
resume       --job-dir <dir> [--json]
import       T.json                       # diagnostics as JSON
repair       T.json [--dry-run --accept-low --accept ID ... --reject ID ... --out-dir D]
preview      --project p.nrproj --out frame.png [--frame N --preview-resolution 720p]
compare      --a a.mp4 --b b.mp4 --out c.mp4 [--mode split|sequential --label-a 2023 --label-b 2024]
watch        --folder IN --preset p.nrproj --out-dir OUT [--once]
scan         [--no-hw-test]
themes | jobs [--clean] | exit-codes
```

With `--json`, progress is printed as one JSON object per line (events such as
`node_started`, `node_progress` with ETA and render fps, `segment_committed`,
`fallback`, `job_done`, `result`, `error`). Exit codes: 0 success; 2 usage;
10–13 import; 20 project; 30–32 FFmpeg/encoder/fallback consent; 40 storage;
50 map tiles; 60 cancelled; 61 render failed; 62 verification failed;
70 plugin (`exit-codes` prints the table).

If a fallback is needed and the CLI is not interactive, the render stops with
exit code 32 unless `--accept-fallback` (or `--allow-placeholder-tiles` for tiles) was given.

## Troubleshooting

| Symptom | What to do |
|---|---|
| "FFmpeg was not found" | Install FFmpeg, or set its folder in Settings → Rendering, then *Test*. |
| Hardware encoder not used | Settings → Rendering → *Test* shows each encoder's test-encode error (often a missing or old GPU driver). |
| Import says "not JSON" / "truncated" | Use *Diagnose & repair…*; import the `.fixed.json` it writes. |
| Map tiles missing | Check the network; the render asks before using placeholders. For no network at all use Plain or MBTiles. |
| Render interrupted | Render Queue → *Interrupted renders* → *Resume*. From the CLI: `resume --job-dir …` (the path is printed when a render is interrupted). |
| "Output file already exists" | Choose another name or confirm overwriting. |
| Verification failed | The file was not added to the library; the job files remain for diagnosis (Advanced diagnostics → Open FFmpeg log). |
| Timeline changed since the project was saved | Re-import the Timeline in the project to confirm the new file. |
| SmartScreen warning | Expected for unsigned builds. |

Logs: *Settings → Storage → Open logs*.

## Architecture

```
src/timelinerx/
  core/        errors (exit-code categories), FallbackDecision/FallbackPolicy, geodesy, easing & filters
  timeline/    value parsing, streaming reader, extractor/parser, outlier filter, column model
  repair/      forensic repair engine and reports
  journeys/    period/route-source selection, filtering, trip legs, pacing
  camera/      framing layer + cinema layer, FramePlan, jerk verification
  maps/        tile providers, disk cache, compositor (zoom cross-fade)
  rendering/   theme node graph & post FX, Qt frame renderer, fonts
  encoding/    FFmpeg discovery, probe, encoder matrix, process control
  pipeline/    render DAG with checkpoints, render job, preview, progress/ETA, watch folder, compare
  audio/       local onset/tempo analysis, audio filter graph
  environment/ hardware scan and recommendation
  projects/    .nrproj model and validation;  storage/ library (SQLite) and settings
  plugins/     plugin loader;  i18n/ en.json, id.json;  ui/ PySide6 desktop UI;  cli/ headless CLI
```

The render pipeline is a DAG (`pipeline/render_job.py`) of 15 nodes: preflight,
analyze_audio, import, normalize, filter, build_journey, plan_camera,
prepare_tiles, prepare_cache, render_frames, encode, verify,
finalize_metadata, generate_thumbnail, add_to_library. Independent nodes run in
parallel. Each node's result is checkpointed with a fingerprint of its inputs
and of its dependencies' results; `render_frames` additionally commits one
encoded segment at a time. More detail: `docs/ARCHITECTURE.md`.

Determinism: identical Timeline, project, map cache and renderer version give
identical frames (fixed grain seed, bundled fonts, no randomness). Every
project and video records `render_engine_version`.

## Testing

```bash
pip install -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen python -m pytest -q
python scripts/make_fixtures.py            # regenerate synthetic fixtures
python scripts/update_visual_baseline.py   # re-record perceptual-hash baseline after an intended visual change
python scripts/benchmark.py                # writes docs/BENCHMARKS.md
```

* **Unit**: coordinate/timestamp parsing (E7, `geo:`), projection, great-circle
  interpolation, easing continuity, zoom-pan interpolation, outlier filter,
  repair engine, presets/projects, resolutions, encoder selection, ETA, i18n
  completeness, log sanitising.
* **Camera verification (Section VI.1)**: for every mode, the maximum
  frame-to-frame acceleration of pan and zoom (viewport units) must stay below
  fixed thresholds; the uncorrected upstream-style path is measured for
  comparison (zoom acceleration is reduced by more than 4×). The marker must
  stay inside the frame; rule-of-thirds must place it behind the centre.
* **Integration**: full renders (all frame rates, aspect ratios), SIGKILL
  during *Encode* followed by resume without re-rendering frames, SIGKILL
  during *Render Frames* followed by resume of the remaining segments,
  cancellation with no surviving FFmpeg process, local tile server (download,
  cache, offline, missing tiles requiring consent), audio beat detection and
  muxing, HDR10 output, comparison videos, CLI exit codes and JSON lines,
  watch folder, GUI smoke test.
* **Visual regression**: DCT perceptual hashes of 20 reference frames compared
  with `tests/baselines/phash.json`; drift above 6 bits raises a warning, not
  a failure.
* **Fixtures**: synthetic only (`scripts/make_fixtures.py`) plus the upstream
  test fixtures. No real Timeline data.

## Licence, third-party notices and attribution

TimelinerX is released under the MIT License (`LICENSE`).

### Attribution

Portions of the timeline parsing, projection, outlier filtering, trip
detection, pacing and camera framing logic are adapted from **Google Timeline
Visualizer** © 2025 mahlernim, MIT License. The upstream copyright notice is
reproduced in `LICENSE`. `THIRD_PARTY_NOTICES.md` lists exactly which parts are
derived, which are new, the third-party dependencies and fonts, and the map
data terms (© OpenStreetMap contributors, © CARTO).

## Known limitations

* The Windows `.exe` has to be built on Windows (PyInstaller does not
  cross-compile). The build scripts and CI workflow are provided; the
  development environment of this release was Linux, where the same spec was
  verified by building and running a Linux executable.
* No GPU encoder was available while developing; NVENC, Quick Sync and AMF
  paths are implemented and selected only after a successful test encode, but
  they were exercised here only through the fallback logic, not on real hardware.
* Rendering is CPU-bound (Qt raster painting). On the 2-core test machine a
  10-second 1080p video took about 40 seconds end to end; see `docs/BENCHMARKS.md`.
* HDR10 export is experimental: the image is graded in SDR and mapped into an
  HDR container; it is not HDR-mastered content.
* Import checkpoints apply to uncompressed `.json` files; `.zip` imports restart from the beginning if interrupted.
* The CARTO tile service's availability and terms are outside this project's control.
* The single-file `.exe` makes replacing the bundled Qt libraries harder; use the one-folder build if you need straightforward LGPL re-linking.
* Ten UI languages. CJK, Arabic and Cyrillic text use system fonts as fallbacks (Yu Gothic / Microsoft YaHei / Segoe UI on Windows), so exact glyph shapes in videos depend on the fonts installed.
* The CARTO key check and keyed tile downloads could not be exercised against CARTO's servers in
  the build environment (no network access to CARTO); the code follows CARTO's documented URL
  format (`…/{z}/{x}/{y}.png?key=…`) and is covered by unit tests with a local tile server.
* The import tutorial draws generic phone screens. Menu names follow Google's current help
  (September 2026) but can differ slightly between phone brands and app versions.
* Cinematic motion blur renders 2–6 sub-frames for moving frames: expect roughly 2–4× longer renders.
