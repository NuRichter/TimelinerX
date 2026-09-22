"""Incremental, resumable scanner for large Timeline JSON files.

Timeline exports are one huge JSON document whose bulk sits in a few top-level
arrays (``semanticSegments``, ``rawSignals``, legacy ``locations`` /
``timelineObjects``, or a bare root array on iOS). This scanner walks the
byte stream, tracks nesting and string state with a regex that jumps between
structural characters, and yields each element of those arrays as an
independent ``json.loads`` call. Because elements are independent, the scan
can stop at any element boundary and later resume from the recorded byte
offset — that is the import checkpoint mechanism of Section X.3.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import BinaryIO, Callable, Iterator, Optional, Tuple

from ..core.errors import InputTooLargeError, UnsupportedFormatError

_STRUCT = re.compile(rb'["\\{}\[\],:]')
_WS = b" \t\r\n"

CHUNK = 4 * 1024 * 1024


@dataclass
class ScanPosition:
    """Where to resume: byte offset of the next element inside ``container``."""

    offset: int
    container: str       # "$root" for a root array, otherwise the root key
    root_kind: str       # "array" | "object"

    def to_dict(self) -> dict:
        return {"offset": self.offset, "container": self.container, "root_kind": self.root_kind}


class ElementScanner:
    def __init__(self, stream: BinaryIO, *, max_element_bytes: int = 256 * 1024 * 1024,
                 resume: Optional[ScanPosition] = None, start_offset: int = 0):
        self.stream = stream
        self.max_element_bytes = max_element_bytes
        self.resume = resume
        self.bytes_read = 0
        self.root_kind: Optional[str] = None
        self.last_boundary: Optional[ScanPosition] = None

    def elements(self) -> Iterator[Tuple[str, object, ScanPosition]]:
        """Yield (container, element, position_after_element)."""
        buf = b""
        base = 0          # absolute offset of buf[0]
        depth = 0
        in_string = False
        skip_at = -1
        key_expected = False
        pending_key: Optional[str] = None
        str_start = -1
        container: Optional[str] = None
        container_depth = -1
        elem_start = -1   # absolute offset
        colon_seen = False

        if self.resume is not None:
            self.stream.seek(self.resume.offset)
            base = self.resume.offset
            self.root_kind = self.resume.root_kind
            container = self.resume.container
            container_depth = 1 if container == "$root" else 2
            depth = container_depth
            elem_start = self.resume.offset
            self.bytes_read = self.resume.offset

        scan_from = 0
        eof = False
        while True:
            if scan_from >= len(buf):
                if eof:
                    break
                chunk = self.stream.read(CHUNK)
                if not chunk:
                    eof = True
                    break
                self.bytes_read += len(chunk)
                buf += chunk
            m_iter = _STRUCT.finditer(buf, scan_from)
            progressed = False
            for m in m_iter:
                pos = m.start()
                ch = buf[pos:pos + 1]
                if in_string:
                    if pos == skip_at:
                        continue
                    if ch == b"\\":
                        skip_at = pos + 1
                        continue
                    if ch == b'"':
                        in_string = False
                        if depth == 1 and self.root_kind == "object" and key_expected and str_start >= 0:
                            try:
                                pending_key = buf[str_start:pos].decode("utf-8")
                            except UnicodeDecodeError:
                                pending_key = None
                            key_expected = False
                            colon_seen = False
                    continue
                if ch == b'"':
                    in_string = True
                    str_start = pos + 1
                    continue
                if ch in (b"{", b"["):
                    if depth == 0:
                        self.root_kind = "object" if ch == b"{" else "array"
                        depth = 1
                        if self.root_kind == "array":
                            container, container_depth = "$root", 1
                            elem_start = base + pos + 1
                        else:
                            key_expected = True
                        continue
                    if (depth == 1 and self.root_kind == "object" and ch == b"["
                            and pending_key is not None and container is None):
                        container, container_depth = pending_key, 2
                        depth = 2
                        elem_start = base + pos + 1
                        continue
                    depth += 1
                    continue
                if ch in (b"}", b"]"):
                    if container is not None and depth == container_depth and ch == b"]":
                        yield from self._emit(buf, base, elem_start, base + pos, container,
                                              base + pos)
                        container = None
                        container_depth = -1
                        pending_key = None
                        depth -= 1
                        continue
                    depth -= 1
                    if depth < 0:
                        raise UnsupportedFormatError("Unbalanced JSON brackets")
                    continue
                if ch == b",":
                    if container is not None and depth == container_depth:
                        yield from self._emit(buf, base, elem_start, base + pos, container,
                                              base + pos + 1)
                        elem_start = base + pos + 1
                    elif depth == 1 and self.root_kind == "object":
                        key_expected = True
                        pending_key = None
                    continue
                if ch == b":":
                    colon_seen = True
                    continue
            # everything scanned; trim the buffer to what an open element still needs
            keep_from = len(buf)
            if container is not None and elem_start >= 0:
                keep_from = min(keep_from, elem_start - base)
            if in_string and str_start >= 0:
                keep_from = min(keep_from, str_start - 1)
            keep_from = max(0, keep_from)
            if keep_from > 0:
                buf = buf[keep_from:]
                base += keep_from
                if skip_at >= 0:
                    skip_at -= keep_from
                if str_start >= 0:
                    str_start -= keep_from
            scan_from = len(buf)
            if container is not None and len(buf) > self.max_element_bytes:
                raise InputTooLargeError(
                    f"A single Timeline record exceeds {self.max_element_bytes // (1024 * 1024)} MB; "
                    "refusing to load it (possible malformed or hostile file).")
            _ = progressed
        if depth != 0 or in_string:
            raise UnsupportedFormatError(
                "The JSON document ended unexpectedly (truncated file?).",
                hint="Run the repair flow: it can recover complete records from a truncated export.")

    def _emit(self, buf: bytes, base: int, start_abs: int, end_abs: int, container: str,
              next_abs: int):
        raw = buf[start_abs - base:end_abs - base].strip(_WS)
        pos = ScanPosition(next_abs, container, self.root_kind or "object")
        self.last_boundary = pos
        if not raw:
            return
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            value = _MalformedElement(len(raw))
        yield container, value, pos


class _MalformedElement:
    def __init__(self, size: int):
        self.size = size


def is_malformed(value: object) -> bool:
    return isinstance(value, _MalformedElement)


ProgressFn = Callable[[float, str], None]
