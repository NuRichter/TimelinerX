"""Record the perceptual-hash baseline for the visual regression test.

Run after reviewing an intended visual change:
    python scripts/update_visual_baseline.py
It also writes the reference frames to tests/baselines/frames/ for human review.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TIMELINERX_HOME", tempfile.mkdtemp(prefix="tlx-vr-"))

from timelinerx.rendering.qt import ensure_qt_app  # noqa: E402
from visual_support import BASELINE, phash, render_cases  # noqa: E402


def main():
    ensure_qt_app()
    frames = render_cases(Path(tempfile.mkdtemp(prefix="tlx-vr-tiles-")))
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    out = {k: phash(v) for k, v in frames.items()}
    BASELINE.write_text(json.dumps(out, indent=1, sort_keys=True), encoding="utf-8")
    fdir = BASELINE.parent / "frames"
    fdir.mkdir(exist_ok=True)
    for k, v in frames.items():
        v.save(fdir / (k.replace("/", "_") + ".png"))
    print(f"baseline: {len(out)} frames → {BASELINE}")


if __name__ == "__main__":
    main()
