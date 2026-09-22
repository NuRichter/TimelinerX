# Third-party notices

This file separates four kinds of material: (a) code derived from the upstream
project, (b) new TimelinerX code, (c) third-party software dependencies, and
(d) third-party map services and data. It describes licences as understood
from the licence files distributed with each component; it is not legal advice.
Where an obligation depends on how *you* redistribute a build, that is stated.

## (a) Components derived from upstream

**Google Timeline Visualizer** — © 2025 mahlernim — MIT License
(full text reproduced in `LICENSE` and `fixtures/upstream/LICENSE-upstream-MIT.txt`).
Reference archives examined: versions 2.4.2 and 3.0.18. Where the two differ,
3.0.18 was used.

Adapted (Python re-implementations of the upstream `visualizer.py` logic, modified):

| TimelinerX module | Upstream concept |
|---|---|
| `core/geo.py` | Web Mercator projection, haversine distance, great-circle interpolation, position-at-distance |
| `timeline/values.py` | coordinate parsing rules (Android `lat°, lon°`, iOS `geo:`, wrappers, E7 in strings) |
| `timeline/parser.py` (`TimelineExtractor._segment`, `finalize`) | semantic-segment extraction, offset-based path timestamps, descending-export detection, standalone-path reconciliation, timezone-missing ordering |
| `timeline/outliers.py` (`find_excursions`) | conservative GPS teleport-excursion filter |
| `journeys/journey.py` | trip-leg detection thresholds, distance-compression pacing (monotone Hermite), Journal-style detailed-first route fusion |
| `camera/planner.py` (framing layer) | camera movement presets, leg-aware context windows, episode-arrival zoom, dead-zone follower, visual-work pacing, overview viewport |

Test fixtures copied unchanged: `fixtures/upstream/*.json` (used for parity tests).

Not used: the Android application (Kotlin UI, lifecycle, `Intent` handling,
MediaCodec exporter), the iOS/web app and its Mediabunny dependency, Play Store
material, and Matplotlib-based rendering. These were not ported; the desktop
UI, renderer and encoder pipeline are new.

## (b) New TimelinerX components

Everything else under `src/timelinerx/` is new in this project,
including: streaming/resumable Timeline reader, legacy Takeout support, ZIP
intake, forensic repair engine, cinema camera layer (viewport-space low-pass,
Catmull-Rom sampling, anticipation, rule-of-thirds lead room, van Wijk–Nuij
zoom-pan interpolation, keyframes), tile providers and cache, colour-grading
node graph and themes, Qt frame renderer, FFmpeg encoder matrix, render DAG
with checkpoints, CLI, watch folder, comparison, audio analysis, plugin SDK,
desktop UI, i18n, tests and packaging. Licensed under the MIT License (`LICENSE`).

## (c) Third-party software

Runtime dependencies (installed via `requirements.txt`; bundled by PyInstaller in the Windows build):

| Component | Version | Licence | Notes |
|---|---|---|---|
| Qt for Python (PySide6, Shiboken6) and the Qt libraries | 6.11.2 | LGPL-3.0 (open-source option) | Used unmodified, dynamically linked. The one-folder build (`build_windows.ps1 -OneDir`) keeps the Qt libraries as separate, replaceable DLLs, which is the straightforward way to meet LGPL re-linking requirements. If you distribute the single-file build, make sure you can still meet them (for example by also providing the one-folder build). Qt source: https://download.qt.io/ |
| NumPy | 2.4.4 | BSD-3-Clause | |
| Pillow | 12.2.0 | MIT-CMU (HPND) | |
| python-dateutil | 2.9.0.post0 | Apache-2.0 / BSD-3-Clause (dual) | |
| psutil | 7.2.2 | BSD-3-Clause | |
| platformdirs | 4.9.6 | MIT | |
| CPython runtime (embedded by PyInstaller) | 3.11 | PSF License | |
| PyInstaller bootloader | 6.22.3 | GPL-2.0 with bootloader exception | The exception allows distributing the generated executable under any licence. |

Development-only (not shipped): pytest (MIT), SciPy (BSD-3-Clause, perceptual hash in tests).

**FFmpeg** is an external program, not included in the repository. TimelinerX
calls `ffmpeg`/`ffprobe` as separate processes. If you bundle FFmpeg
(`build_windows.ps1 -FFmpegDir …`), the licence of that FFmpeg build applies to
your distribution: LGPL-2.1+ builds require providing the corresponding FFmpeg
source; builds with `--enable-gpl` (which include libx264/libx265) are GPL-2.0+
and require offering the complete corresponding source of that FFmpeg build.
x264 and x265 are GPL-2.0+. HEVC and H.264 may be subject to patent licensing in
some jurisdictions. See https://ffmpeg.org/legal.html.

Bundled fonts (`assets/fonts/`, licence texts alongside):

| Font | Licence |
|---|---|
| Instrument Sans (Regular, Bold) — © 2022 The Instrument Sans Project Authors | SIL Open Font License 1.1 (`InstrumentSans-OFL.txt`) |
| Outfit (Regular, Bold) — © 2021 The Outfit Project Authors | SIL Open Font License 1.1 (`Outfit-OFL.txt`) |
| DejaVu Sans (Regular, Bold) | Bitstream Vera / DejaVu licence, public-domain changes (`DejaVu-LICENSE.txt`) |

Icons in `ui/icons.py` and `assets/icons/` were drawn for this project.

## (d) Map services and data

* **CARTO basemaps** (`carto-light`, `carto-dark`, `carto-voyager` providers).
  Tiles © CARTO, map data © OpenStreetMap contributors (ODbL 1.0,
  https://www.openstreetmap.org/copyright). The attribution
  "© OpenStreetMap contributors © CARTO" is rendered into every video that uses
  these tiles and cannot be disabled while they are used. CARTO basemap usage
  is subject to CARTO's terms (https://carto.com/legal/); free usage has
  limits, and commercial use may require an agreement and an API key
  (`CARTO_BASEMAP_API_KEY`). TimelinerX requests tiles at a limited rate with an
  identifying User-Agent and caches them locally.
* **MBTiles provider**: you supply the file and are responsible for its licence;
  its `attribution` metadata is rendered into the video.
* **Plain provider**: no third-party data.

The Timeline file you import is processed locally and is never sent to any map
provider. However, requesting tiles (zoom/x/y) necessarily tells the tile
server which map areas the video shows, i.e. roughly where the route goes. If
that matters to you, use the Plain provider or an MBTiles file (no network
requests), or offline mode with a cache you filled earlier.
