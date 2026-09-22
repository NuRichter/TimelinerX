"""Video Library — metadata only (never Timeline content), stored in SQLite."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

from ..utils.paths import data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    thumbnail TEXT,
    project_path TEXT,
    date_start TEXT,
    date_end TEXT,
    duration_s REAL,
    width INTEGER,
    height INTEGER,
    fps INTEGER,
    size_bytes INTEGER,
    codec TEXT,
    encoder TEXT,
    theme TEXT,
    camera_mode TEXT,
    render_engine_version TEXT,
    created_at REAL,
    extra TEXT
);
"""


@dataclass
class VideoEntry:
    title: str
    path: str
    thumbnail: Optional[str] = None
    project_path: Optional[str] = None
    date_start: Optional[str] = None
    date_end: Optional[str] = None
    duration_s: float = 0.0
    width: int = 0
    height: int = 0
    fps: int = 0
    size_bytes: int = 0
    codec: str = ""
    encoder: str = ""
    theme: str = ""
    camera_mode: str = ""
    render_engine_version: str = ""
    created_at: float = 0.0
    extra: str = "{}"
    id: Optional[int] = None

    @property
    def exists(self) -> bool:
        return Path(self.path).is_file()


class Library:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else data_dir() / "library.sqlite3"
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def add(self, e: VideoEntry) -> int:
        d = asdict(e)
        d.pop("id")
        d["created_at"] = d["created_at"] or time.time()
        with self._lock, self._conn() as c:
            cols = ",".join(d)
            q = ",".join("?" for _ in d)
            upd = ",".join(f"{k}=excluded.{k}" for k in d if k != "path")
            cur = c.execute(f"INSERT INTO videos ({cols}) VALUES ({q}) "
                            f"ON CONFLICT(path) DO UPDATE SET {upd}", list(d.values()))
            row = c.execute("SELECT id FROM videos WHERE path=?", (e.path,)).fetchone()
            return int(row["id"]) if row else int(cur.lastrowid)

    def list(self, search: str = "") -> List[VideoEntry]:
        with self._conn() as c:
            if search:
                rows = c.execute("SELECT * FROM videos WHERE title LIKE ? ORDER BY created_at DESC",
                                 (f"%{search}%",)).fetchall()
            else:
                rows = c.execute("SELECT * FROM videos ORDER BY created_at DESC").fetchall()
        return [VideoEntry(**dict(r)) for r in rows]

    def get(self, vid: int) -> Optional[VideoEntry]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
        return VideoEntry(**dict(r)) if r else None

    def remove(self, vid: int, delete_file: bool = False) -> None:
        """Caller must obtain explicit user confirmation before calling this."""
        e = self.get(vid)
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM videos WHERE id=?", (vid,))
        if e and delete_file:
            for p in (e.path, e.thumbnail, e.path + ".nrmeta.json"):
                if p:
                    try:
                        Path(p).unlink()
                    except FileNotFoundError:
                        pass
