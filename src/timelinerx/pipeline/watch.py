"""Watch folder (Section VIII.2).

Polls a user-configured folder (never a default) for new Timeline exports
(.json / .zip). A file is processed once its size and mtime are stable across
two polls. Each file is rendered with a preset project whose Timeline is
replaced; every action is written to ``watch.log`` and reported through the
``notify`` callback (desktop notification in the GUI, stdout in the CLI).
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from ..core.errors import TimelinerXError
from ..core.fallback import FallbackPolicy
from ..projects.project import Project, TimelineRef
from ..timeline.parser import sha256_file
from ..utils.cancel import CancelToken
from ..utils.paths import safe_filename
from .render_job import RenderJob

Notify = Callable[[str, str], None]  # (title, message)


class WatchFolder:
    def __init__(self, folder: Path, preset: Project, out_dir: Path, log_path: Path,
                 notify: Optional[Notify] = None, accept_fallback: bool = False,
                 cancel: Optional[CancelToken] = None, poll_s: float = 5.0):
        self.folder = Path(folder)
        if not self.folder.is_dir():
            raise TimelinerXError(f"Watch folder does not exist: {self.folder}")
        self.preset = preset
        self.out_dir = Path(out_dir)
        self.log_path = Path(log_path)
        self.notify = notify or (lambda t, m: None)
        self.accept_fallback = accept_fallback
        self.cancel = cancel or CancelToken()
        self.poll_s = poll_s
        self._seen: Dict[str, Tuple[int, float]] = {}
        self._pending: Dict[str, Tuple[int, float]] = {}
        self._state_file = self.log_path.with_suffix(".state.json")
        try:
            self._done = set(json.loads(self._state_file.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            self._done = set()

    def _log(self, msg: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")

    def poll_once(self) -> list:
        """Returns the list of output paths rendered during this poll."""
        rendered = []
        for p in sorted(self.folder.iterdir()):
            if p.suffix.lower() not in (".json", ".zip") or p.name.endswith(".fixed.json"):
                continue
            st = p.stat()
            sig = (st.st_size, st.st_mtime)
            key = str(p.resolve())
            if f"{key}|{sig}" in self._done:
                continue
            if self._pending.get(key) != sig:
                self._pending[key] = sig          # wait one more poll for the copy to finish
                continue
            self._pending.pop(key, None)
            out = self._render(p)
            self._done.add(f"{key}|{sig}")
            self._state_file.write_text(json.dumps(sorted(self._done)), encoding="utf-8")
            if out:
                rendered.append(out)
        return rendered

    def _render(self, p: Path) -> Optional[Path]:
        proj = Project.from_dict(self.preset.to_dict())
        proj.timeline = TimelineRef(str(p), sha256_file(p), p.stat().st_size)
        proj.name = f"{self.preset.name} - {p.stem}"
        out = self.out_dir / f"{safe_filename(proj.name)}.mp4"
        self._log(f"START {p.name} → {out}")
        self.notify("TimelinerX", f"Automatic render started for {p.name}")
        try:
            RenderJob(proj, out, policy=FallbackPolicy(accept_all=self.accept_fallback),
                      cancel=self.cancel).run()
        except TimelinerXError as e:
            self._log(f"FAIL  {p.name}: {e}")
            self.notify("TimelinerX", f"Automatic render failed for {p.name}: {e}")
            return None
        self._log(f"DONE  {p.name} → {out}")
        self.notify("TimelinerX", f"Automatic render finished: {out.name}")
        return out

    def run_forever(self) -> None:
        self._log(f"WATCH {self.folder}")
        while not self.cancel.cancelled:
            try:
                self.poll_once()
            except OSError as e:
                self._log(f"ERROR {e}")
            self.cancel.wait(self.poll_s)
