"""Environment scan (Section XI).

Collects OS, CPU, RAM, GPU/VRAM, free storage and FFmpeg capabilities and
derives a *recommendation profile*. Everything reported is labelled either
"capacity information" (measured) or "estimated recommendation" (derived) —
nothing here is a performance guarantee.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from ..core.errors import FfmpegUnavailableError
from ..encoding import ffmpeg as ff

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None


@dataclass
class GpuInfo:
    name: str
    vendor: str
    vram_mb: Optional[int] = None
    source: str = ""


@dataclass
class EnvironmentReport:
    os: str
    python: str
    cpu: str
    cpu_cores_physical: Optional[int]
    cpu_cores_logical: Optional[int]
    ram_total_gb: Optional[float]
    ram_available_gb: Optional[float]
    gpus: List[GpuInfo] = field(default_factory=list)
    storage_free_gb: Optional[float] = None
    storage_path: str = ""
    ffmpeg: Optional[dict] = None
    ffmpeg_error: Optional[str] = None
    profile: str = "Comfortable"
    profile_reasons: List[str] = field(default_factory=list)
    recommended: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["labels"] = {"capacity_information": ["os", "cpu", "ram", "gpus", "storage", "ffmpeg"],
                       "estimated_recommendation": ["profile", "recommended"],
                       "disclaimer": "Recommendations are estimates from detected capacity, not guarantees."}
        return d


def _run(args: List[str], timeout: float = 8.0) -> str:
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout,
                           creationflags=ff.CREATE_NO_WINDOW)
        return r.stdout.decode(errors="replace") if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _vendor(name: str) -> str:
    n = name.lower()
    if "nvidia" in n or "geforce" in n or "quadro" in n or "rtx" in n:
        return "NVIDIA"
    if "intel" in n:
        return "Intel"
    if "amd" in n or "radeon" in n or "ati " in n:
        return "AMD"
    return "Other"


def detect_gpus() -> List[GpuInfo]:
    gpus: List[GpuInfo] = []
    smi = shutil.which("nvidia-smi")
    if smi:
        out = _run([smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and parts[1].isdigit():
                gpus.append(GpuInfo(parts[0], "NVIDIA", int(parts[1]), "nvidia-smi"))
    if sys.platform == "win32":
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if ps:
            out = _run([ps, "-NoProfile", "-NonInteractive", "-Command",
                        "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM | "
                        "ConvertTo-Json -Compress"], timeout=15)
            try:
                data = json.loads(out) if out.strip() else []
                if isinstance(data, dict):
                    data = [data]
                for g in data:
                    name = str(g.get("Name", "")).strip()
                    if not name or any(x.name == name for x in gpus):
                        continue
                    ram = g.get("AdapterRAM")
                    # AdapterRAM is a 32-bit field and saturates at 4 GB; mark it as a lower bound
                    vram = int(ram) // 2 ** 20 if isinstance(ram, int) and ram > 0 else None
                    gpus.append(GpuInfo(name, _vendor(name), vram,
                                        "WMI (values ≥4096 MB are a lower bound)"))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
    elif sys.platform.startswith("linux"):
        out = _run(["lspci"]) if shutil.which("lspci") else ""
        for line in out.splitlines():
            if re.search(r"VGA|3D controller|Display controller", line):
                name = line.split(":", 2)[-1].strip()
                if not any(x.name in name for x in gpus):
                    gpus.append(GpuInfo(name, _vendor(name), None, "lspci"))
    elif sys.platform == "darwin":
        out = _run(["system_profiler", "SPDisplaysDataType"], timeout=15)
        for m in re.finditer(r"Chipset Model:\s*(.+)", out):
            gpus.append(GpuInfo(m.group(1).strip(), _vendor(m.group(1)), None, "system_profiler"))
    return gpus


def cpu_name() -> str:
    if sys.platform == "win32":
        return platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "unknown")
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def scan(storage_path: Optional[str] = None, ffmpeg_path: Optional[str] = None,
         verify_hw: bool = True) -> EnvironmentReport:
    sp = Path(storage_path or Path.home())
    ram_total = ram_avail = None
    phys = logical = None
    if psutil:
        vm = psutil.virtual_memory()
        ram_total, ram_avail = vm.total / 2 ** 30, vm.available / 2 ** 30
        phys, logical = psutil.cpu_count(False), psutil.cpu_count(True)
    else:
        logical = os.cpu_count()
    try:
        free = shutil.disk_usage(sp).free / 2 ** 30
    except OSError:
        free = None
    rep = EnvironmentReport(os=f"{platform.system()} {platform.release()} ({platform.version()})",
                            python=platform.python_version(), cpu=cpu_name(), cpu_cores_physical=phys,
                            cpu_cores_logical=logical,
                            ram_total_gb=round(ram_total, 1) if ram_total else None,
                            ram_available_gb=round(ram_avail, 1) if ram_avail else None,
                            gpus=detect_gpus(), storage_free_gb=round(free, 1) if free is not None else None,
                            storage_path=str(sp))
    try:
        info = ff.probe(ffmpeg_path, verify_hw=verify_hw)
        rep.ffmpeg = info.to_dict()
    except FfmpegUnavailableError as e:
        rep.ffmpeg_error = str(e) + (f" {e.hint}" if e.hint else "")
    recommend(rep)
    return rep


def recommend(rep: EnvironmentReport) -> None:
    """Comfortable / Heavy / Extreme with the reasoning spelled out."""
    reasons = []
    ram = rep.ram_total_gb or 0
    vram = max((g.vram_mb or 0) for g in rep.gpus) if rep.gpus else 0
    hw = [e for e, ok in (rep.ffmpeg or {}).get("hw_verified", {}).items() if ok]
    cores = rep.cpu_cores_logical or 1
    score = 0
    if ram >= 32:
        score += 2
        reasons.append(f"RAM {ram:.0f} GB ≥ 32 GB (+2)")
    elif ram >= 16:
        score += 1
        reasons.append(f"RAM {ram:.0f} GB ≥ 16 GB (+1)")
    else:
        reasons.append(f"RAM {ram:.0f} GB < 16 GB (+0)")
    if vram >= 8192:
        score += 2
        reasons.append(f"VRAM {vram} MB ≥ 8 GB (+2)")
    elif vram >= 4096:
        score += 1
        reasons.append(f"VRAM {vram} MB ≥ 4 GB (+1)")
    else:
        reasons.append("VRAM unknown or < 4 GB (+0)")
    if hw:
        score += 1
        reasons.append(f"verified hardware encoder(s): {', '.join(hw)} (+1)")
    else:
        reasons.append("no verified hardware encoder (+0)")
    if cores >= 12:
        score += 1
        reasons.append(f"{cores} logical CPU cores ≥ 12 (+1)")
    profile = "Extreme" if score >= 5 else "Heavy" if score >= 3 else "Comfortable"
    rep.profile = profile
    rep.profile_reasons = reasons + [f"score {score}: ≥5 Extreme, 3–4 Heavy, ≤2 Comfortable"]
    rep.recommended = {
        "Comfortable": {"max_resolution": "1080p", "fps": 30, "quality": "high"},
        "Heavy": {"max_resolution": "1440p", "fps": 60, "quality": "high"},
        "Extreme": {"max_resolution": "2160p", "fps": 60, "quality": "cinematic"},
    }[profile]
    if rep.storage_free_gb is not None and rep.storage_free_gb < 5:
        rep.profile_reasons.append(f"only {rep.storage_free_gb} GB free storage: high resolutions may not fit")
