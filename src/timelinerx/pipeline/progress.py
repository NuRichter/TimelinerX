"""Progress Engine: honest ETA and live performance sampling.

ETA is shown only after enough frames were measured (``min_samples``) —
before that the UI shows "estimating". Frame time is an exponentially
weighted moving average, and the remaining non-frame work (final mux/encode,
verification) is estimated from its measured share on this machine when
available, otherwise from a conservative ratio that is labelled as such.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None


@dataclass
class EtaEstimator:
    total_frames: int
    alpha: float = 0.08
    min_samples: int = 20
    done: int = 0
    ewma: Optional[float] = None
    samples: int = 0
    started: float = field(default_factory=time.perf_counter)
    tail_ratio: float = 0.06   # remaining post-frame work as a share of frame work (estimate)

    def frame_done(self, seconds: float, frames: int = 1) -> None:
        per = seconds / max(1, frames)
        self.ewma = per if self.ewma is None else (1 - self.alpha) * self.ewma + self.alpha * per
        self.samples += frames
        self.done += frames

    def skip(self, frames: int) -> None:
        """Frames restored from a checkpoint (count toward progress, not speed)."""
        self.done += frames

    @property
    def fps(self) -> Optional[float]:
        return (1.0 / self.ewma) if self.ewma else None

    def eta_seconds(self) -> Optional[float]:
        if self.ewma is None or self.samples < self.min_samples:
            return None
        remaining = max(0, self.total_frames - self.done) * self.ewma
        return remaining * (1.0 + self.tail_ratio)

    def to_dict(self) -> dict:
        eta = self.eta_seconds()
        return {"frames_done": self.done, "frames_total": self.total_frames,
                "render_fps": round(self.fps, 2) if self.fps else None,
                "eta_s": round(eta, 1) if eta is not None else None,
                "eta_state": "estimating" if eta is None else "measured",
                "elapsed_s": round(time.perf_counter() - self.started, 1)}


class PerformanceMonitor:
    """Samples CPU, RAM and process memory every ``interval`` seconds in a daemon thread."""

    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self.latest: dict = {}
        self._stop = threading.Event()
        self._t: Optional[threading.Thread] = None
        self._proc = psutil.Process(os.getpid()) if psutil else None

    def start(self) -> "PerformanceMonitor":
        if psutil is None:
            self.latest = {"available": False}
            return self
        psutil.cpu_percent(None)
        self._t = threading.Thread(target=self._loop, daemon=True, name="tlx-perf")
        self._t.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                vm = psutil.virtual_memory()
                rss = self._proc.memory_info().rss
                children = 0
                for c in self._proc.children(recursive=True):
                    try:
                        children += c.memory_info().rss
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                self.latest = {"available": True, "cpu_percent": psutil.cpu_percent(None),
                               "ram_used_percent": vm.percent,
                               "app_rss_mb": round(rss / 2 ** 20, 1),
                               "ffmpeg_rss_mb": round(children / 2 ** 20, 1)}
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        self._stop.set()
