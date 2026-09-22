# Changelog

All notable changes to this project are documented here.

## [1.0.0] — 2026-09-22 (render engine `tlx-2.0.0`)

The app is now **TimelinerX** (previously NuRichter Timeliner). Project files keep the `.nrproj`
extension; 0.x projects open unchanged and old option names are migrated. Settings and the video
library are copied once from the 0.x data folder; the tile cache is not (it may contain
watermarked CARTO tiles).

### Added
- Brand: logo in the sidebar, splash screen, application icon (all derived from the master logo
  files by `scripts/make_brand_assets.py`); palette aligned with the logo.
- Journey Builder: **Zoom style** (Fixed, Balanced, Active, Close-Up), **Long-trip detection**
  (Conservative, Balanced, Sensitive — each button shows how many long trips it finds),
  **Local trip framing** (Off, Balanced, Close), **Long-trip pacing** (Natural, Balanced, Faster,
  Fastest); a live route sketch; calm/lively duration estimates with one-click apply.
- Transport modes from the export (walk, bike/motorbike, car/bus, train, ferry, flight); flights
  are drawn as arcs flown by a plane marker and dashed in the overview; flights without a label are
  inferred from physics only (≥ 150 km at ≥ 250 km/h).
- Settings: distance unit (kilometres / miles) for the whole app and new videos; CARTO API key with
  an online verification; restart prompt when the language changes.
- Motion-graphic import tutorial (Android and iPhone), in the app and exportable as MP4
  (`timelinerx tutorial`).
- About page with an interactive "how it works" flowchart and credits
  (Made with NuRichter Workspace · LinkedIn · GitHub).
- Eight more UI languages: Español, Français, Deutsch, Português (Brasil), Русский, العربية
  (right-to-left layout), 中文（简体）, 日本語 — with localised dates, numbers and units in videos.
- Video: optional cinematic motion blur (180° shutter, adaptive sub-frames); date ribbon;
  ending-card figures that count up.
- CLI: `--zoom-style`, `--long-trip-detection`, `--local-framing`, `--long-trip-pacing`,
  `--motion-blur`, `--unit`, `--carto-key`, `tutorial`.

### Changed
- Camera: visual work is measured at every route vertex (busy local episodes no longer race across
  the screen); a final screen-speed equalisation re-times the journey against the real camera;
  intro/outro length scales with the zoom distance; the marker-visibility guard is low-passed
  instead of clamped. On the reference timeline (24,209 km, 41 long trips, 30 s @ 60 fps) the peak
  on-screen marker speed dropped from 101 to 53 px/frame and the 99th percentile from 61 to 50.
- Long-trip pacing now has a real effect with visual pacing (upstream ignores it there).
- CARTO: tiles are always requested with a key; without a key the render refuses (every unkeyed
  tile carries an "API KEY REQUIRED" watermark) and the preview shows the plain background with a
  notice. Tiles are cached per key, so watermarked tiles can never reach a keyed render.

## [0.1.0] — 2026-09-20 (render engine `nrte-1.0.0`)

## [0.1.0] — 2026-09-20 (render engine `nrte-1.0.0`)

### Added
- Desktop application (PySide6) with Dashboard, Timeline Import, Timeline Analysis, Journey Builder,
  Visual Settings, Video Settings, Preview, Render Queue, Video Library and Settings.
- Headless CLI (`render`, `resume`, `import`, `repair`, `preview`, `compare`, `watch`, `scan`, `new-project`,
  `themes`, `jobs`, `exit-codes`) with JSON-lines progress and category exit codes.
- Watch folder (opt-in) with logging and desktop notifications.
- Split-screen and sequential comparison of two renders.
- Plugin SDK: JSON theme plugins, Python map-provider plugins (opt-in, validated).
- English and Bahasa Indonesia UI; light/dark appearance; reduced-motion preference.

### Rendering
- Cinema camera layer over the upstream framing logic: viewport-space zero-phase smoothing,
  Catmull-Rom sampling, anticipation, rule-of-thirds lead room, van Wijk–Nuij zoom-pan intro/outro,
  Director's-Cut keyframes, marker visibility guard; new *Active* camera mode.
- Ten themes plus an experimental slot as JSON colour-grading node graphs; tile grading cached on disk.
- Zoom-level cross-fading between tile pyramids; great-circle densified trails; gradient trail,
  selective bloom, soft shadow, pulse marker, vignette, deterministic grain; bundled fonts; title layouts
  and ending card inside a 5 % safe area.
- Render pipeline as a checkpointed DAG with segment-level resume after crashes or power loss.
- Encoder matrix NVENC → Quick Sync → AMF → software with test encodes and explicit fallback decisions;
  optional two-pass; experimental HDR10 (PQ) export; output verification before library entry.
- Optional user audio with local onset/tempo detection, beat-aligned ending, ducking and fade-out.

### Import
- Streaming, resumable parser for large files; legacy Takeout `Records.json` and Semantic Location History;
  Takeout ZIP intake with safety limits; detailed diagnostics.

### Repair
- Ten-pass forensic repair with HIGH/MEDIUM/LOW confidence classification, fixed copy and
  HTML/JSON/text reports; the source file is never modified.

### Packaging
- PyInstaller spec producing a windowed app and a console CLI executable; Windows build scripts with
  optional FFmpeg bundling and code signing; GitHub Actions Windows workflow.

### Documentation
- README, THIRD_PARTY_NOTICES, architecture and plugin guides, measured benchmarks.
