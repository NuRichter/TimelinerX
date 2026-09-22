"""Local audio analysis for optional beat-synced pacing (Section VI.6).

The user's own audio file is decoded by FFmpeg to mono 22.05 kHz PCM and
analysed on this machine only: a half-wave-rectified spectral-flux onset
envelope, adaptive-threshold peak picking, and a tempo estimate from the
envelope autocorrelation. No network service is involved.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import numpy as np

from ..core.errors import TimelinerXError
from ..encoding.ffmpeg import FfmpegInfo, run

SR = 22050
HOP = 512
WIN = 1024


@dataclass
class AudioAnalysis:
    duration_s: float
    onsets_s: List[float] = field(default_factory=list)
    tempo_bpm: Optional[float] = None
    rms_db: float = -99.0

    def to_dict(self) -> dict:
        return {"duration_s": round(self.duration_s, 3), "onsets": len(self.onsets_s),
                "onsets_s": [round(o, 3) for o in self.onsets_s[:2000]],
                "tempo_bpm": round(self.tempo_bpm, 1) if self.tempo_bpm else None,
                "rms_db": round(self.rms_db, 1)}


def decode_pcm(info: FfmpegInfo, path: Path, max_seconds: float = 3600) -> np.ndarray:
    args = [info.ffmpeg, "-hide_banner", "-v", "error", "-nostdin", "-t", str(max_seconds),
            "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    r = run(args, timeout=300)
    if r.returncode != 0:
        raise TimelinerXError("Could not decode the audio file: "
                             + r.stderr.decode(errors="replace").strip()[-300:])
    return np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768.0


def analyze_signal(x: np.ndarray, sr: int = SR) -> AudioAnalysis:
    dur = len(x) / sr
    if len(x) < WIN * 4:
        return AudioAnalysis(dur)
    rms = float(np.sqrt(np.mean(x ** 2)) + 1e-12)
    n_frames = 1 + (len(x) - WIN) // HOP
    idx = np.arange(WIN)[None, :] + HOP * np.arange(n_frames)[:, None]
    frames = x[idx] * np.hanning(WIN)[None, :]
    mag = np.abs(np.fft.rfft(frames, axis=1))
    logmag = np.log1p(10.0 * mag)
    flux = np.maximum(0.0, np.diff(logmag, axis=0)).sum(axis=1)
    flux = np.concatenate([[0.0], flux])
    if flux.max() <= 0:
        return AudioAnalysis(dur, rms_db=20 * np.log10(rms))
    env = flux / flux.max()
    # adaptive threshold: local median + offset
    w = max(3, int(0.2 * sr / HOP))
    pad = np.pad(env, (w, w), mode="edge")
    local = np.array([np.median(pad[i:i + 2 * w + 1]) for i in range(len(env))])
    thr = local + 0.08
    peaks = []
    min_gap = int(0.1 * sr / HOP)
    last = -min_gap
    for i in range(1, len(env) - 1):
        if env[i] > thr[i] and env[i] >= env[i - 1] and env[i] >= env[i + 1] and i - last >= min_gap:
            peaks.append(i)
            last = i
    onsets = [p * HOP / sr for p in peaks]
    # tempo via autocorrelation of the onset envelope in 60–180 BPM
    e = env - env.mean()
    ac = np.correlate(e, e, mode="full")[len(e) - 1:]
    fps = sr / HOP
    lo, hi = int(fps * 60 / 180), int(fps * 60 / 60)
    tempo = None
    if hi < len(ac) and hi > lo:
        lag = lo + int(np.argmax(ac[lo:hi]))
        if ac[lag] > 0:
            tempo = 60.0 * fps / lag
    return AudioAnalysis(dur, onsets, tempo, 20 * np.log10(rms))


def analyze_file(info: FfmpegInfo, path: Path) -> AudioAnalysis:
    return analyze_signal(decode_pcm(info, path))


def nearest_onset(onsets: List[float], target_s: float, window_s: float = 0.75) -> Optional[float]:
    best = None
    for o in onsets:
        if abs(o - target_s) <= window_s and (best is None or abs(o - target_s) < abs(best - target_s)):
            best = o
    return best


def audio_filter(duration_s: float, volume: float, fade_out_s: float, duck_windows: List[tuple],
                 duck_db: float) -> str:
    """ffmpeg -af chain: pad/trim to the video, volume, ducking windows, fade out."""
    parts = ["apad", f"atrim=0:{duration_s:.3f}", f"volume={max(0.0, volume):.3f}"]
    gain = 10 ** (duck_db / 20.0)
    for a, b in duck_windows:
        parts.append(f"volume={gain:.4f}:enable='between(t,{a:.3f},{b:.3f})'")
    if fade_out_s > 0:
        st = max(0.0, duration_s - fade_out_s)
        parts.append(f"afade=t=out:st={st:.3f}:d={fade_out_s:.3f}")
    return ",".join(parts)
