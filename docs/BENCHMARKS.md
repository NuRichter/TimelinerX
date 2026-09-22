# Benchmarks

Measured 2026-09-20 by `scripts/benchmark.py` on: Linux 6.18.44-fc-v37, Intel(R) Xeon(R) Processor @ 2.80GHz, 2 logical CPUs, 7.8 GB RAM, Python 3.11.15. No GPU encoder was available on this machine, so all encoding used libx264 (software).

These are the numbers as measured on that machine — not tuned, not extrapolated. Re-run the script on your own hardware; expect very different figures on a desktop with more cores and a hardware encoder.

## Timeline parsing

| File size | Points | Mode | Time | Throughput |
|---|---|---|---|---|
| 10.0 MB | 118,459 | in-memory | 0.83 s | 12.1 MB/s |
| 50.0 MB | 591,811 | streaming | 8.6 s | 5.8 MB/s |
| 100.0 MB | 1,183,501 | streaming | 17.77 s | 5.6 MB/s |

## Frame rendering (renderer only, no encoding)

Synthetic in-process map tiles, standard test route, steady state (tiles already fetched and graded — in a real render the Prepare Tiles / Prepare Cache stages do that up front).

| Resolution | Size | Theme | ms / frame | Frames / s |
|---|---|---|---|---|
| 480p | 852x480 | light | 17.4 | 57.4 |
| 480p | 852x480 | neon_dark_blue | 20.8 | 48.1 |
| 1080p | 1920x1080 | light | 70.2 | 14.2 |
| 1080p | 1920x1080 | neon_dark_blue | 87.4 | 11.4 |
| 1440p | 2560x1440 | light | 113.2 | 8.8 |
| 1440p | 2560x1440 | neon_dark_blue | 150.7 | 6.6 |

## End to end (render + libx264 encode + verify)

Plain provider (no map tiles), neon theme, quality *high*, 30 fps.

| Resolution | Video length | Wall time | Speed vs real time |
|---|---|---|---|
| 720p | 10 s | 19.7 s | 0.51× |
| 1080p | 10 s | 41.6 s | 0.24× |
