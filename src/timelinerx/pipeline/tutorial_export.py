"""Render the import tutorial motion graphic to an MP4 (same drawing code as the in-app player)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional

from ..core.errors import RenderCancelledError, RenderFailedError
from ..encoding import ffmpeg as ff


def export_tutorial(out: Path, platform: str = "android", lang: Optional[str] = None, *, fps: int = 30,
                    width: int = 1920, height: int = 1080, theme: str = "dark",
                    ffmpeg_path: Optional[str] = None,
                    progress: Optional[Callable[[float], None]] = None, cancel=None) -> Path:
    from PySide6.QtGui import QImage, QPainter

    from ..i18n import language, set_language
    from ..rendering.qt import ensure_qt_app, family
    from ..ui.tutorial_scene import Palette, TutorialScene
    ensure_qt_app()
    prev = language()
    if lang:
        set_language(lang)
    try:
        scene = TutorialScene(platform, Palette.dark() if theme == "dark" else Palette.light(), family("ui"),
                              rtl=(lang or prev) == "ar")
        info = ff.probe(ffmpeg_path, verify_hw=False)
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.stem + ".part.mp4")
        args = [info.ffmpeg, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{width}x{height}",
                "-r", str(fps), "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-preset",
                "medium", "-movflags", "+faststart", str(tmp)]
        proc = ff.popen(args, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        n = int(round(scene.duration * fps))
        img = QImage(width, height, QImage.Format_RGB32)
        try:
            for f in range(n):
                if cancel is not None and getattr(cancel, "cancelled", False):
                    raise RenderCancelledError("Tutorial export cancelled.")
                p = QPainter(img)
                scene.draw(p, f / fps, width, height)
                p.end()
                proc.stdin.write(bytes(img.constBits())[: width * height * 4] if img.bytesPerLine() == width * 4
                                 else b"".join(bytes(img.constScanLine(y))[: width * 4] for y in range(height)))
                if progress and f % 15 == 0:
                    progress(f / n)
            proc.stdin.close()
            err = proc.stderr.read().decode(errors="replace")
            rc = proc.wait()
        except BaseException:
            ff._kill(proc)
            tmp.unlink(missing_ok=True)
            raise
        finally:
            ff._unregister(proc)
        if rc != 0 or not tmp.is_file():
            tmp.unlink(missing_ok=True)
            raise RenderFailedError(f"FFmpeg could not encode the tutorial video: {err.strip()[-400:]}")
        tmp.replace(out)
        if progress:
            progress(1.0)
        return out
    finally:
        set_language(prev)
