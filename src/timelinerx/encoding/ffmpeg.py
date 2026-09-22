"""FFmpeg discovery, capability probing, encoder selection and process control.

* Discovery order: configured path → bundled next to the executable
  (``ffmpeg/ffmpeg.exe``) → ``PATH``.
* Capability probe lists encoders/filters *and* runs a 0.5 s test encode for
  each hardware encoder, because an encoder can be compiled in while the GPU
  or driver is missing — a name in ``-encoders`` proves nothing.
* Encoder selection follows the matrix NVENC → Quick Sync → AMF → software.
  Every step down the matrix produces a :class:`FallbackDecision`; if the user
  explicitly chose an encoder, the decision requires confirmation.
* Every subprocess is started with an argument list (never a shell string) and
  registered, so cancellation and interpreter exit kill children — no orphan
  ffmpeg processes.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, IO, List, Optional, Sequence, Tuple

from ..core.errors import (EncoderUnavailableError, FfmpegUnavailableError, RenderCancelledError,
                           RenderFailedError, UsageError)
from ..core.fallback import FallbackDecision, FallbackPolicy

ENCODER_MATRIX: Dict[str, List[Tuple[str, str]]] = {
    "h264": [("nvenc", "h264_nvenc"), ("qsv", "h264_qsv"), ("amf", "h264_amf"), ("software", "libx264")],
    "hevc": [("nvenc", "hevc_nvenc"), ("qsv", "hevc_qsv"), ("amf", "hevc_amf"), ("software", "libx265")],
}
FAMILY_LABEL = {"nvenc": "NVIDIA NVENC", "qsv": "Intel Quick Sync", "amf": "AMD AMF/VCN",
                "software": "Software (x264/x265)"}
MAX_DIM = {"h264_nvenc": 4096, "hevc_nvenc": 8192, "h264_qsv": 4096, "hevc_qsv": 8192,
           "h264_amf": 4096, "hevc_amf": 8192, "libx264": 8192, "libx265": 8192}

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# --------------------------------------------------------- process registry
_PROCS: "set[subprocess.Popen]" = set()
_PROCS_LOCK = threading.Lock()


def _register(p: subprocess.Popen) -> None:
    with _PROCS_LOCK:
        _PROCS.add(p)


def _unregister(p: subprocess.Popen) -> None:
    with _PROCS_LOCK:
        _PROCS.discard(p)


def kill_all_children() -> int:
    with _PROCS_LOCK:
        procs = list(_PROCS)
    for p in procs:
        _kill(p)
    return len(procs)


def live_children() -> int:
    with _PROCS_LOCK:
        return sum(1 for p in _PROCS if p.poll() is None)


def _kill(p: subprocess.Popen) -> None:
    try:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass
    finally:
        _unregister(p)


atexit.register(kill_all_children)


# ----- OS-level orphan protection (works even if this process is SIGKILLed / crashes)
_WIN_JOB = None


def _windows_job():
    """A Job Object with KILL_ON_JOB_CLOSE: Windows kills every assigned child when our
    last handle closes — including when this process is terminated abruptly."""
    global _WIN_JOB
    if _WIN_JOB is not None or sys.platform != "win32":
        return _WIN_JOB
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32.CreateJobObjectW.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        _WIN_JOB = (k32, job)
    except Exception:  # noqa: BLE001 — protection is best-effort; registry + atexit still apply
        _WIN_JOB = None
    return _WIN_JOB


def _assign_to_job(p: subprocess.Popen) -> None:
    j = _windows_job()
    if j is None:
        return
    k32, job = j
    try:
        k32.AssignProcessToJobObject(job, int(p._handle))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def _linux_pdeathsig():  # runs in the child between fork and exec
    try:
        import ctypes
        import signal
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001
        pass


def popen(args: Sequence[str], **kw) -> subprocess.Popen:
    if not isinstance(args, (list, tuple)) or not all(isinstance(a, str) for a in args):
        raise TypeError("subprocess arguments must be a list of strings (no shell strings)")
    kw.setdefault("creationflags", CREATE_NO_WINDOW)
    if sys.platform.startswith("linux"):
        kw.setdefault("preexec_fn", _linux_pdeathsig)
    p = subprocess.Popen(list(args), shell=False, **kw)
    if sys.platform == "win32":
        _assign_to_job(p)
    _register(p)
    return p


def run(args: Sequence[str], timeout: float = 120, **kw) -> subprocess.CompletedProcess:
    p = popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(p)
        raise
    finally:
        _unregister(p)
    return subprocess.CompletedProcess(list(args), p.returncode, out, err)


# ---------------------------------------------------------------- discovery
def locate(tool: str = "ffmpeg", configured: Optional[str] = None) -> Optional[str]:
    exe = tool + (".exe" if sys.platform == "win32" else "")
    cands: List[Path] = []
    if configured:
        c = Path(configured)
        cands.append(c if c.name.lower().startswith(tool) else c / exe)
    env = os.environ.get("TIMELINERX_FFMPEG_DIR") or os.environ.get("NURICHTER_FFMPEG_DIR")
    if env:
        cands.append(Path(env) / exe)
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    cands += [base / "ffmpeg" / exe, Path(sys.executable).parent / "ffmpeg" / exe]
    for c in cands:
        if c.is_file():
            return str(c)
    return shutil.which(tool)


@dataclass
class FfmpegInfo:
    ffmpeg: str
    ffprobe: Optional[str]
    version: str
    encoders: set = field(default_factory=set)
    filters: set = field(default_factory=set)
    pix_fmts: set = field(default_factory=set)
    hw_verified: Dict[str, bool] = field(default_factory=dict)
    hw_errors: Dict[str, str] = field(default_factory=dict)

    def has_encoder(self, name: str) -> bool:
        return name in self.encoders and self.hw_verified.get(name, True)

    def to_dict(self) -> dict:
        return {"ffmpeg": self.ffmpeg, "ffprobe": self.ffprobe, "version": self.version,
                "encoders": sorted(e for fam in ENCODER_MATRIX.values() for _, e in fam if e in self.encoders),
                "hw_verified": self.hw_verified, "hw_errors": self.hw_errors,
                "zscale": "zscale" in self.filters}


_PROBE_CACHE: Dict[Tuple[str, bool], FfmpegInfo] = {}


def probe(configured: Optional[str] = None, verify_hw: bool = True, force: bool = False) -> FfmpegInfo:
    path = locate("ffmpeg", configured)
    if not path:
        raise FfmpegUnavailableError(
            "FFmpeg was not found.",
            hint="Install FFmpeg and add it to PATH, place ffmpeg.exe in an 'ffmpeg' folder next to "
                 "TimelinerX.exe, or set its location in Settings → Rendering.")
    if not force:
        if (path, True) in _PROBE_CACHE:
            return _PROBE_CACHE[(path, True)]
        if not verify_hw and (path, False) in _PROBE_CACHE:
            return _PROBE_CACHE[(path, False)]
    try:
        v = run([path, "-hide_banner", "-version"], timeout=20)
        enc = run([path, "-hide_banner", "-encoders"], timeout=20)
        flt = run([path, "-hide_banner", "-filters"], timeout=20)
        pfm = run([path, "-hide_banner", "-pix_fmts"], timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise FfmpegUnavailableError(f"FFmpeg at {path} could not be executed: {e}") from e
    if v.returncode != 0:
        raise FfmpegUnavailableError(f"FFmpeg at {path} failed to report its version.")
    version = v.stdout.decode(errors="replace").splitlines()[0] if v.stdout else "unknown"
    encoders = set(re.findall(r"^\s*[VAS][\w.]{5}\s+(\S+)", enc.stdout.decode(errors="replace"), re.M))
    filters = set(re.findall(r"^\s*[\w.]{2,3}\s+(\S+)\s+\S+->\S+", flt.stdout.decode(errors="replace"), re.M))
    pix = set(re.findall(r"^[IOHPB.]{5}\s+(\S+)", pfm.stdout.decode(errors="replace"), re.M))
    info = FfmpegInfo(path, locate("ffprobe", configured and str(Path(configured).parent)), version,
                      encoders, filters, pix)
    for fam in ENCODER_MATRIX.values():
        for family, name in fam:
            if family == "software" or name not in encoders:
                continue
            if verify_hw:
                ok, err = _test_encode(path, name)
                info.hw_verified[name] = ok
                if not ok:
                    info.hw_errors[name] = err
            else:
                info.hw_verified[name] = False
                info.hw_errors[name] = "not verified"
    _PROBE_CACHE[(path, verify_hw)] = info
    return info


def _test_encode(ffmpeg: str, encoder: str) -> Tuple[bool, str]:
    args = [ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
            "color=c=black:s=256x256:r=30:d=0.5", "-c:v", encoder, "-f", "null", "-"]
    try:
        r = run(args, timeout=25)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if r.returncode == 0:
        return True, ""
    lines = [l.strip() for l in r.stderr.decode(errors="replace").splitlines() if l.strip()]
    key = [l for l in lines if any(w in l.lower() for w in ("cannot", "failed", "error", "not found",
                                                           "no device", "unsupported", "driver"))]
    msg = (key[0] if key else (lines[0] if lines else f"exit code {r.returncode}"))
    return False, msg[:300]


# ---------------------------------------------------------------- selection
@dataclass
class EncoderChoice:
    codec: str
    family: str
    name: str
    decisions: List[FallbackDecision] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{FAMILY_LABEL[self.family]} ({self.name})"


def select_encoder(info: FfmpegInfo, codec: str, preference: str, width: int, height: int,
                   policy: FallbackPolicy) -> EncoderChoice:
    if codec not in ENCODER_MATRIX:
        raise UsageError(f"Unknown codec {codec!r}")
    matrix = ENCODER_MATRIX[codec]

    def usable(name: str) -> Tuple[bool, str]:
        if name not in info.encoders:
            return False, "not included in this FFmpeg build"
        if not info.hw_verified.get(name, True):
            return False, "test encode failed: " + info.hw_errors.get(name, "unknown error")
        if max(width, height) > MAX_DIM[name]:
            return False, f"supports at most {MAX_DIM[name]} px per side"
        return True, ""

    if preference not in ("auto",) + tuple(f for f, _ in matrix):
        raise UsageError(f"Unknown encoder preference {preference!r}")
    start = 0 if preference == "auto" else [f for f, _ in matrix].index(preference)
    requested_family, requested_name = matrix[start]
    decisions: List[FallbackDecision] = []
    for i in range(start, len(matrix)):
        family, name = matrix[i]
        ok, why = usable(name)
        if ok:
            if i != start or (preference == "auto" and i > 0):
                skipped = matrix[start:i] if preference != "auto" else matrix[0:i]
                reasons = "; ".join(f"{FAMILY_LABEL[f]}: {usable(n)[1]}" for f, n in skipped)
                d = FallbackDecision(
                    subsystem="encoder",
                    requested=FAMILY_LABEL[requested_family] if preference != "auto" else "auto (best available)",
                    proposed=f"{FAMILY_LABEL[family]} ({name})", reason=reasons,
                    impact=("Software encoding is slower but produces equivalent or better quality."
                            if family == "software" else "Different hardware encoder; similar quality."),
                    requires_confirmation=preference != "auto")
                policy.resolve(d)
                decisions.append(d)
            return EncoderChoice(codec, family, name, decisions)
        if i == len(matrix) - 1:
            raise EncoderUnavailableError(
                f"No usable {codec.upper()} encoder: {FAMILY_LABEL[family]} {why}.",
                hint="Use an FFmpeg build that includes libx264/libx265.")
    raise EncoderUnavailableError("No encoder available")  # pragma: no cover


QUALITY_TABLE = {
    #            x264 crf, preset,     nvenc cq, preset, qsv q, amf qp
    "draft":     (26, "veryfast", 29, "p2", 28, 28),
    "standard":  (21, "medium",   24, "p4", 24, 24),
    "high":      (18, "slow",     20, "p6", 21, 21),
    "cinematic": (16, "slower",   18, "p7", 19, 19),
}
BITS_PER_PIXEL = {"draft": 0.05, "standard": 0.08, "high": 0.11, "cinematic": 0.15}


def encoder_args(choice: EncoderChoice, quality: str, fps: int, *, mezzanine: bool = False) -> List[str]:
    crf, preset, cq, npreset, qq, aq = QUALITY_TABLE[quality]
    gop = str(max(1, fps * 2))
    color = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
             "-color_range", "tv"]
    if mezzanine:
        # visually-lossless intermediate used when a final re-encode follows (two-pass / HDR)
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "8", "-pix_fmt", "yuv420p",
                "-g", gop] + color
    n = choice.name
    if n == "libx264":
        a = ["-c:v", n, "-preset", preset, "-crf", str(crf), "-profile:v", "high", "-pix_fmt", "yuv420p"]
    elif n == "libx265":
        a = ["-c:v", n, "-preset", preset, "-crf", str(crf + 2), "-pix_fmt", "yuv420p", "-tag:v", "hvc1",
             "-x265-params", "log-level=error"]
    elif n.endswith("_nvenc"):
        a = ["-c:v", n, "-preset", npreset, "-rc", "vbr", "-cq", str(cq), "-b:v", "0",
             "-spatial-aq", "1", "-pix_fmt", "yuv420p"]
        if n.startswith("hevc"):
            a += ["-tag:v", "hvc1"]
    elif n.endswith("_qsv"):
        a = ["-c:v", n, "-preset", {"veryfast": "veryfast", "medium": "medium", "slow": "slow",
                                    "slower": "veryslow"}[preset],
             "-global_quality", str(qq), "-pix_fmt", "nv12"]
        if n.startswith("hevc"):
            a += ["-tag:v", "hvc1"]
    elif n.endswith("_amf"):
        a = ["-c:v", n, "-quality", "quality" if quality in ("high", "cinematic") else "balanced",
             "-rc", "cqp", "-qp_i", str(aq), "-qp_p", str(aq + 2), "-pix_fmt", "yuv420p"]
        if n.startswith("hevc"):
            a += ["-tag:v", "hvc1"]
    else:  # pragma: no cover
        raise EncoderUnavailableError(n)
    return a + ["-g", gop] + color


# ------------------------------------------------------------- raw encoding
class FrameEncoder:
    """Pipes BGRA frames into an ffmpeg process."""

    def __init__(self, info: FfmpegInfo, out_path: Path, width: int, height: int, fps: int,
                 codec_args: List[str], log_path: Optional[Path] = None, container: str = "matroska"):
        self.out_path = Path(out_path)
        self.frame_bytes = width * height * 4
        args = [info.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{width}x{height}", "-r", str(fps),
                "-i", "pipe:0", "-an",
                "-vf", "scale=out_color_matrix=bt709:out_range=tv:flags=accurate_rnd+full_chroma_int",
                *codec_args, "-f", container, str(self.out_path)]
        self.args = args
        self._log = open(log_path, "ab") if log_path else subprocess.DEVNULL
        self.proc = popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self._stderr: List[bytes] = []
        self._t = threading.Thread(target=self._drain, daemon=True)
        self._t.start()

    def _drain(self):
        for line in iter(self.proc.stderr.readline, b""):
            self._stderr.append(line)
            if self._log is not subprocess.DEVNULL:
                self._log.write(line)
        self.proc.stderr.close()

    def write(self, frame: memoryview | bytes) -> None:
        if len(frame) != self.frame_bytes:
            raise RenderFailedError(f"Frame has {len(frame)} bytes, expected {self.frame_bytes}")
        try:
            self.proc.stdin.write(frame)
        except (BrokenPipeError, OSError) as e:
            self.abort()
            raise RenderFailedError("FFmpeg stopped accepting frames: " + self.stderr_tail()) from e

    def close(self, timeout: float = 600) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            rc = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.abort()
            raise RenderFailedError("FFmpeg did not finish in time.")
        self._t.join(timeout=5)
        _unregister(self.proc)
        if self._log is not subprocess.DEVNULL:
            self._log.close()
        if rc != 0:
            raise RenderFailedError(f"FFmpeg exited with code {rc}: {self.stderr_tail()}")

    def abort(self) -> None:
        _kill(self.proc)
        if self._log is not subprocess.DEVNULL:
            try:
                self._log.close()
            except OSError:
                pass

    def stderr_tail(self, n: int = 6) -> str:
        return b"".join(self._stderr[-n:]).decode(errors="replace").strip()



# SDR (BT.709) → HDR10 (BT.2020 / PQ) mappings, most accurate first. zimg versions differ in what
# they accept (e.g. some Windows builds reject a Y'CbCr→linear step with "Generic error in an
# external library"), so the first chain that survives a real one-frame test encode is used.
HDR_CHAINS = [
    "zscale=tin=709:min=709:pin=709:rin=limited:t=linear:npl=203,format=gbrpf32le,"
    "zscale=p=2020:t=smpte2084:m=2020_ncl:r=limited,format=yuv420p10le",
    "zscale=tin=709:min=709:pin=709:rin=limited:m=gbr:r=full,format=gbrpf32le,"
    "zscale=t=linear:npl=203,zscale=p=2020:t=smpte2084:m=2020_ncl:r=limited,format=yuv420p10le",
    "zscale=tin=709:min=709:pin=709:rin=limited:p=2020:t=smpte2084:m=2020_ncl:r=limited:npl=203,"
    "format=yuv420p10le",
]
HDR_X265 = ("log-level=error:hdr10=1:repeat-headers=1:colorprim=bt2020:transfer=smpte2084:"
            "colormatrix=bt2020nc:max-cll=203,203:"
            "master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)")
_HDR_CACHE: Dict[str, Tuple[Optional[str], str]] = {}


def hdr_chain(info: "FfmpegInfo", width: int = 1280, height: int = 720) -> Tuple[Optional[str], str]:
    """(working filter chain or None, reason).

    Verified the way the render will use it: a short clip is first encoded with the exact
    mezzanine settings (x264, yuv420p, BT.709 tags) at the render size, then decoded through each
    candidate chain into libx265 HDR10. A synthetic source is not enough — some zimg builds accept
    it but reject real decoded frames."""
    key = f"{info.ffmpeg}|{width}x{height}"
    if key in _HDR_CACHE:
        return _HDR_CACHE[key]
    if "zscale" not in info.filters or "libx265" not in info.encoders:
        res = (None, "this FFmpeg build lacks the zscale filter or libx265")
        _HDR_CACHE[key] = res
        return res
    import tempfile
    last = ""
    with tempfile.TemporaryDirectory(prefix="tlx-hdr-") as td:
        sample = Path(td) / "mezz.mp4"
        mk = run([info.ffmpeg, "-hide_banner", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                  f"testsrc2=s={width}x{height}:r=30:d=0.2", "-frames:v", "4",
                  *encoder_args(EncoderChoice("h264", "software", "libx264"), "draft", 30, mezzanine=True),
                  str(sample)], timeout=60)
        if mk.returncode != 0 or not sample.is_file():
            res = (None, "could not prepare an HDR test clip: "
                   + (mk.stderr.decode(errors="replace").strip().splitlines() or [""])[-1][:160])
            _HDR_CACHE[key] = res
            return res
        for chain in HDR_CHAINS:
            args = [info.ffmpeg, "-hide_banner", "-v", "error", "-nostdin", "-i", str(sample),
                    "-vf", chain, "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", HDR_X265,
                    "-f", "null", "-"]
            try:
                r = run(args, timeout=90)
            except (OSError, subprocess.TimeoutExpired) as e:
                last = str(e)
                continue
            if r.returncode == 0:
                _HDR_CACHE[key] = (chain, "")
                return _HDR_CACHE[key]
            last = (r.stderr.decode(errors="replace").strip().splitlines() or [""])[-1]
    res = (None, f"no SDR→HDR10 conversion worked in a test encode with this FFmpeg ({last[:160]})")
    _HDR_CACHE[key] = res
    return res


def hdr_chain_candidates(first: Optional[str]) -> List[str]:
    return ([first] if first else []) + [c for c in HDR_CHAINS if c != first]


def write_metadata_file(path: Path, **tags: str) -> List[str]:
    """Write container tags to an FFMETADATA1 file (UTF-8) and return the input args for it.

    Non-ASCII titles are never put on the command line: on some platforms/locales argv is
    encoded with a legacy code page and the value would be mangled or rejected. Returns
    ``["-f", "ffmetadata", "-i", path]``; the caller adds ``-map_metadata <input index>``.
    """
    def esc(v: str) -> str:
        for ch in ("\\", "=", ";", "#", "\n"):
            v = v.replace(ch, "\\" + ch)
        return v
    body = ";FFMETADATA1\n" + "".join(f"{esc(k)}={esc(str(v))}\n" for k, v in tags.items())
    path.write_text(body, encoding="utf-8", newline="\n")
    return ["-f", "ffmetadata", "-i", str(path)]

def run_ffmpeg(info: FfmpegInfo, args: List[str], cancel=None, log_path: Optional[Path] = None,
               progress_cb=None, total_s: Optional[float] = None) -> None:
    """Run ffmpeg with ``-progress`` reporting; honours cancellation."""
    full = [info.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-progress", "pipe:1", *args]
    log = open(log_path, "ab") if log_path else None
    p = popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err: List[bytes] = []

    def drain():
        for line in iter(p.stderr.readline, b""):
            err.append(line)
            if log:
                log.write(line)

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    try:
        for raw in iter(p.stdout.readline, b""):
            if cancel is not None and cancel.cancelled:
                _kill(p)
                raise RenderCancelledError(cancel.reason or "cancelled")
            line = raw.decode(errors="replace").strip()
            if progress_cb and total_s and line.startswith("out_time_us="):
                try:
                    us = int(line.split("=", 1)[1])
                    progress_cb(min(1.0, us / 1e6 / total_s))
                except ValueError:
                    pass
        rc = p.wait()
    finally:
        t.join(timeout=5)
        _unregister(p)
        if log:
            log.close()
    if rc != 0:
        raise RenderFailedError("FFmpeg failed: " + b"".join(err[-8:]).decode(errors="replace").strip())


def ffprobe_json(info: FfmpegInfo, path: Path, count_packets: bool = True) -> dict:
    if not info.ffprobe:
        raise FfmpegUnavailableError("ffprobe was not found next to ffmpeg; cannot verify the output.")
    args = [info.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams"]
    if count_packets:
        args.append("-count_packets")
    args.append(str(path))
    r = run(args, timeout=300)
    if r.returncode != 0:
        raise RenderFailedError("ffprobe failed: " + r.stderr.decode(errors="replace")[-400:])
    return json.loads(r.stdout.decode())
