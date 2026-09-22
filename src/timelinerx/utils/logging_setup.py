"""Logging with coordinate sanitisation (Section XIV).

Unless the user explicitly enables local debug logging of coordinates, any
number pair that looks like a latitude/longitude is replaced by ``<coord>``
before a record reaches a handler.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_COORD = re.compile(r"-?\d{1,3}\.\d{3,}\s*°?\s*,\s*-?\d{1,3}\.\d{3,}\s*°?")
_E7 = re.compile(r"(latitudeE7|longitudeE7|latE7|lngE7)\"?\s*[:=]\s*-?\d+")


def sanitize(text: str) -> str:
    return _E7.sub(r"\1=<coord>", _COORD.sub("<coord>", text))


class CoordinateFilter(logging.Filter):
    def __init__(self, allow_coordinates: bool = False):
        super().__init__()
        self.allow = allow_coordinates

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.allow:
            msg = record.getMessage()
            record.msg = sanitize(msg)
            record.args = ()
        return True


def setup_logging(log_dir: Path, allow_coordinates: bool = False, level: int = logging.INFO) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "timelinerx.log"
    root = logging.getLogger("timelinerx")
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    h = RotatingFileHandler(path, maxBytes=5 * 2 ** 20, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    h.addFilter(CoordinateFilter(allow_coordinates))
    root.addHandler(h)
    return path


log = logging.getLogger("timelinerx")
