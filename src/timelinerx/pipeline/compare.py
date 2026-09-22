"""Multi-project comparison (Section VIII.7).

Combines two rendered videos either side by side (split screen, each half
labelled) or one after the other with a generated title card between them.
The inputs are ordinary NuRichter renders, so this reuses the whole pipeline
rather than duplicating render logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

from ..core.errors import UsageError
from ..encoding import ffmpeg as ff
from ..utils.cancel import CancelToken


def _probe_video(info, p: Path) -> dict:
    j = ff.ffprobe_json(info, p, count_packets=False)
    v = next(s for s in j["streams"] if s["codec_type"] == "video")
    n, _, d = v["r_frame_rate"].partition("/")
    return {"w": int(v["width"]), "h": int(v["height"]), "fps": float(n) / float(d or 1),
            "dur": float(j["format"]["duration"])}


def title_card_png(path: Path, width: int, height: int, title: str, subtitle: str) -> None:
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter

    from ..rendering.qt import ensure_qt_app, family
    ensure_qt_app()
    img = QImage(width, height, QImage.Format_RGB32)
    img.fill(QColor("#0b0d14"))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    s = min(width, height) / 1080
    f = QFont(family("display"))
    f.setPixelSize(int(80 * s))
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("#f4f6ff"))
    p.drawText(QRectF(0, height * 0.36, width, 110 * s), Qt.AlignHCenter | Qt.AlignVCenter, title)
    f2 = QFont(family("ui"))
    f2.setPixelSize(int(38 * s))
    p.setFont(f2)
    p.setPen(QColor("#a9b0c8"))
    p.drawText(QRectF(0, height * 0.36 + 120 * s, width, 60 * s), Qt.AlignHCenter | Qt.AlignVCenter, subtitle)
    p.end()
    img.save(str(path))


def compare(a: Path, b: Path, out: Path, mode: str = "split", label_a: str = "A", label_b: str = "B",
            ffmpeg_path: Optional[str] = None, cancel: Optional[CancelToken] = None,
            title: str = "Comparison") -> Path:
    info = ff.probe(ffmpeg_path, verify_hw=False)
    va, vb = _probe_video(info, a), _probe_video(info, b)
    if abs(va["fps"] - vb["fps"]) > 0.01:
        raise UsageError(f"Frame rates differ ({va['fps']} vs {vb['fps']}); render both at the same FPS.")
    fps = va["fps"]
    tmpdir = out.parent / (out.stem + ".compare-tmp")
    tmpdir.mkdir(parents=True, exist_ok=True)
    try:
        if mode == "split":
            h = min(va["h"], vb["h"])
            h -= h % 2
            flt = (f"[0:v]scale=-2:{h},setsar=1[a];[1:v]scale=-2:{h},setsar=1[b];"
                   f"[a][b]hstack=inputs=2,format=yuv420p[v]")
            dur = max(va["dur"], vb["dur"])
            args = (["-i", str(a), "-i", str(b)] + ff.write_metadata_file(tmpdir / "meta.txt", title=title)
                    + ["-filter_complex", flt, "-map", "[v]", "-map_metadata", "2",
                       "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-t", f"{dur:.3f}",
                       "-movflags", "+faststart", str(out)])
        elif mode == "sequential":
            w, h = va["w"], va["h"]
            cards = []
            for i, (lab, v) in enumerate(((label_a, va), (label_b, vb))):
                png = tmpdir / f"card{i}.png"
                title_card_png(png, w, h, lab, title)
                cards.append(png)
            parts = [cards[0], a, cards[1], b]
            inputs = []
            chains = []
            for i, p in enumerate(parts):
                if str(p).endswith(".png"):
                    inputs += ["-loop", "1", "-t", "2.5", "-framerate", f"{fps}", "-i", str(p)]
                else:
                    inputs += ["-i", str(p)]
                chains.append(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v{i}]")
            flt = ";".join(chains) + ";" + "".join(f"[v{i}]" for i in range(4)) + "concat=n=4:v=1:a=0[v]"
            inputs += ff.write_metadata_file(tmpdir / "meta.txt", title=title)
            args = inputs + ["-filter_complex", flt, "-map", "[v]", "-map_metadata", "4", "-c:v", "libx264",
                             "-crf", "18", "-preset", "medium", "-movflags", "+faststart", str(out)]
        else:
            raise UsageError("mode must be 'split' or 'sequential'")
        ff.run_ffmpeg(info, args, cancel)
    finally:
        for f in tmpdir.glob("*"):
            f.unlink()
        tmpdir.rmdir()
    return out
