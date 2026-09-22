"""Map tile providers and the on-disk tile cache.

Provider contract (also the Plugin SDK contract, Section VIII.3)::

    class MapProviderPlugin(Protocol):
        id: str; name: str; max_zoom: int; tile_size: int
        def fetch_tile(self, z, x, y) -> TileResult
        def attribution(self) -> str
        def rate_limit(self) -> RateLimitPolicy

Built-in providers
* ``carto-light`` / ``carto-dark`` / ``carto-voyager`` — CARTO basemaps
  (OpenStreetMap data). Network access, attribution
  "© OpenStreetMap contributors © CARTO". CARTO requires an API key: without
  one every tile is watermarked, so rendering refuses to start (see
  ``maps/keys.py`` for where the key comes from).
* ``mbtiles:<path>`` — an offline raster MBTiles file you own.
* ``plain`` — no map tiles at all; a flat themed background with a
  graticule. It is only used when the user selects it explicitly.

Failed tiles are never silently replaced: :class:`TileSet` records them and
the render pipeline asks for consent (FallbackDecision) before drawing a
placeholder.
"""

from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Protocol, Set, Tuple

from .. import __version__
from ..core.errors import TileProviderError

TileKey = Tuple[int, int, int]  # z, x, y
USER_AGENT = f"TimelinerX/{__version__} (desktop application; local-first)"


@dataclass(frozen=True)
class RateLimitPolicy:
    requests_per_second: float = 8.0
    max_concurrency: int = 4


@dataclass
class TileResult:
    ok: bool
    data: Optional[bytes] = None   # encoded PNG/JPEG/WebP
    error: str = ""


class MapProvider(Protocol):
    id: str
    name: str
    max_zoom: int
    tile_size: int
    base_flavor: str  # light | dark | none

    def fetch_tile(self, z: int, x: int, y: int) -> TileResult: ...

    def attribution(self) -> str: ...

    def rate_limit(self) -> RateLimitPolicy: ...


class CartoProvider:
    STYLES = {"light": "light_all", "dark": "dark_all", "voyager": "rastertiles/voyager"}

    def __init__(self, flavor: str = "light", api_key: Optional[str] = None, timeout: float = 15.0,
                 base_url: Optional[str] = None):
        from .keys import fingerprint, resolve_key
        if flavor not in self.STYLES:
            raise ValueError(flavor)
        self.id = f"carto-{flavor}"
        self.name = f"CARTO {flavor.title()}"
        self.max_zoom = 19
        self.tile_size = 256
        self.base_flavor = "dark" if flavor == "dark" else "light"
        self._style = self.STYLES[flavor]
        self._key = (api_key if api_key is not None else resolve_key()).strip()
        self._timeout = timeout
        self._base = base_url  # test hook / self-hosted mirror
        # CARTO's own CDN watermarks unkeyed tiles; a custom mirror decides for itself
        self.requires_key = base_url is None
        self.has_key = bool(self._key)
        self._fp = fingerprint(self._key)

    @property
    def cache_id(self) -> str:
        """Disk-cache namespace: provider + key fingerprint (watermarked unkeyed tiles never mix
        with keyed ones)."""
        return f"{self.id}@{self._fp}" if self.requires_key else self.id

    def url(self, z: int, x: int, y: int) -> str:
        if self._base:
            u = self._base.format(z=z, x=x, y=y, style=self._style)
        else:
            u = f"https://basemaps.cartocdn.com/{self._style}/{z}/{x}/{y}.png"
        if self._key:
            u += ("&" if "?" in u else "?") + "key=" + urllib.request.quote(self._key)
        return u

    def fetch_tile(self, z: int, x: int, y: int) -> TileResult:
        req = urllib.request.Request(self.url(z, x, y), headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                data = r.read(4 * 1024 * 1024 + 1)
                if len(data) > 4 * 1024 * 1024:
                    return TileResult(False, error="tile larger than 4 MB")
                return TileResult(True, data)
        except urllib.error.HTTPError as e:
            return TileResult(False, error=f"HTTP {e.code}")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            return TileResult(False, error=str(getattr(e, "reason", e)))

    def attribution(self) -> str:
        return "© OpenStreetMap contributors © CARTO"

    def rate_limit(self) -> RateLimitPolicy:
        return RateLimitPolicy(8.0, 4)


class MBTilesProvider:
    def __init__(self, path: str, attribution: str = ""):
        p = Path(path)
        if not p.is_file():
            raise TileProviderError(f"MBTiles file not found: {p}")
        self.path = p
        self.id = "mbtiles:" + hashlib.sha1(str(p.resolve()).encode()).hexdigest()[:10]
        self._local = threading.local()
        meta = dict(self._conn().execute("SELECT name, value FROM metadata").fetchall())
        self.name = meta.get("name", p.stem)
        fmt = meta.get("format", "png")
        if fmt not in ("png", "jpg", "jpeg", "webp"):
            raise TileProviderError(f"MBTiles format '{fmt}' is not a raster format.")
        self.max_zoom = int(meta.get("maxzoom", 18))
        self.tile_size = 256
        self.base_flavor = "light"
        self._attr = attribution or meta.get("attribution", "") or f"Map tiles: {p.name}"

    def _conn(self):
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            self._local.c = c
        return c

    def fetch_tile(self, z, x, y) -> TileResult:
        tms_y = (1 << z) - 1 - y
        row = self._conn().execute(
            "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
            (z, x, tms_y)).fetchone()
        if row is None:
            return TileResult(False, error="tile not in MBTiles file")
        return TileResult(True, bytes(row[0]))

    def attribution(self) -> str:
        return self._attr

    def rate_limit(self) -> RateLimitPolicy:
        return RateLimitPolicy(10_000.0, 8)


class PlainProvider:
    """No tiles. Chosen explicitly by the user (offline, or a minimal look)."""

    id = "plain"
    name = "Plain (no map tiles)"
    max_zoom = 22
    tile_size = 256
    base_flavor = "none"

    def fetch_tile(self, z, x, y) -> TileResult:
        return TileResult(False, error="plain provider has no tiles")

    def attribution(self) -> str:
        return ""

    def rate_limit(self) -> RateLimitPolicy:
        return RateLimitPolicy(1e9, 1)


PLUGIN_PROVIDERS: Dict[str, Callable[[], "MapProvider"]] = {}


def cache_namespace(provider) -> str:
    return str(getattr(provider, "cache_id", provider.id)).replace(":", "_").replace("@", "_")


def missing_key(provider) -> bool:
    """True when the provider needs an API key that is not configured."""
    return bool(getattr(provider, "requires_key", False)) and not getattr(provider, "has_key", True)


def make_provider(spec: str, plugins: Optional[Dict[str, Callable[[], MapProvider]]] = None) -> MapProvider:
    if spec in ("carto-light", "carto"):
        return CartoProvider("light")
    if spec == "carto-dark":
        return CartoProvider("dark")
    if spec == "carto-voyager":
        return CartoProvider("voyager")
    if spec == "plain":
        return PlainProvider()
    if spec.startswith("mbtiles:"):
        return MBTilesProvider(spec[len("mbtiles:"):])
    plugins = plugins if plugins is not None else PLUGIN_PROVIDERS
    if spec in plugins:
        return plugins[spec]()
    raise TileProviderError(f"Unknown map provider '{spec}'.")


class _TokenBucket:
    def __init__(self, rate: float):
        self.rate = max(0.1, rate)
        self.tokens = self.rate
        self.t = time.monotonic()
        self.lock = threading.Lock()

    def take(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.rate, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            time.sleep(wait)


@dataclass
class PrefetchReport:
    requested: int = 0
    from_cache: int = 0
    downloaded: int = 0
    failed: Dict[TileKey, str] = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def missing(self) -> int:
        return len(self.failed)


class TileCache:
    """Disk cache ``<root>/<provider-id>/<z>/<x>/<y>.tile`` — never deleted implicitly."""

    def __init__(self, provider: MapProvider, root: Path, offline: bool = False):
        self.provider = provider
        self.root = Path(root) / cache_namespace(provider)
        self.offline = offline
        self._bucket = _TokenBucket(provider.rate_limit().requests_per_second)
        self.failed: Dict[TileKey, str] = {}

    def path(self, key: TileKey) -> Path:
        z, x, y = key
        return self.root / str(z) / str(x) / f"{y}.tile"

    def get_bytes(self, key: TileKey) -> Optional[bytes]:
        p = self.path(key)
        if p.is_file():
            try:
                return p.read_bytes()
            except OSError:
                return None
        return None

    def fetch(self, key: TileKey) -> Tuple[Optional[bytes], str]:
        cached = self.get_bytes(key)
        if cached is not None:
            return cached, "cache"
        if isinstance(self.provider, PlainProvider):
            return None, "plain"
        if self.offline and not isinstance(self.provider, MBTilesProvider):
            self.failed[key] = "offline mode: tile not cached"
            return None, "offline"
        attempt = 0
        res = TileResult(False, error="not attempted")
        while attempt < 3:
            self._bucket.take()
            res = self.provider.fetch_tile(*key)
            if res.ok or (res.error.startswith("HTTP 4") and res.error != "HTTP 429"):
                break
            attempt += 1
            time.sleep(0.4 * (2 ** attempt))
        if not res.ok or res.data is None:
            self.failed[key] = res.error
            return None, "failed"
        if not isinstance(self.provider, MBTilesProvider):
            p = self.path(key)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(f".{threading.get_ident()}.part")
            tmp.write_bytes(res.data)
            os.replace(tmp, p)
        return res.data, "network"

    def prefetch(self, keys: Iterable[TileKey], progress: Optional[Callable[[int, int], None]] = None,
                 cancel=None) -> PrefetchReport:
        keys = sorted(set(keys))
        rep = PrefetchReport(requested=len(keys))
        t0 = time.perf_counter()
        todo = []
        for k in keys:
            if self.path(k).is_file():
                rep.from_cache += 1
            else:
                todo.append(k)
        done = rep.from_cache
        if progress:
            progress(done, len(keys))
        if todo and not isinstance(self.provider, PlainProvider):
            workers = max(1, self.provider.rate_limit().max_concurrency)
            with ThreadPoolExecutor(workers) as ex:
                for k, (data, src) in zip(todo, ex.map(self.fetch, todo)):
                    if cancel is not None and cancel.cancelled:
                        break
                    done += 1
                    if data is None:
                        rep.failed[k] = self.failed.get(k, src)
                    elif src == "network":
                        rep.downloaded += 1
                    else:
                        rep.from_cache += 1
                    if progress and done % 16 == 0:
                        progress(done, len(keys))
        if progress:
            progress(len(keys), len(keys))
        rep.seconds = time.perf_counter() - t0
        return rep

    def usage_bytes(self) -> int:
        total = 0
        if self.root.exists():
            for p in self.root.rglob("*.tile"):
                total += p.stat().st_size
        return total


def tiles_for_viewport(cx: float, cy: float, span_x: float, span_y: float, zoom: int) -> List[TileKey]:
    from ..core.geo import MAX_EXTENT, WORLD_SPAN
    n = 1 << zoom
    size = WORLD_SPAN / n
    x0 = int((cx - span_x / 2 + MAX_EXTENT) // size)
    x1 = int((cx + span_x / 2 + MAX_EXTENT) // size)
    y0 = int((MAX_EXTENT - (cy + span_y / 2)) // size)
    y1 = int((MAX_EXTENT - (cy - span_y / 2)) // size)
    out = []
    for ty in range(max(0, y0), min(n - 1, y1) + 1):
        for tx in range(x0, x1 + 1):
            out.append((zoom, tx % n, ty))
    return out


def ideal_zoom(span_x: float, width_px: int, tile_size: int = 256, bias: float = 0.0) -> float:
    """Fractional zoom where one tile pixel ≈ one output pixel."""
    import math
    from ..core.geo import WORLD_SPAN
    return math.log2(max(1e-9, WORLD_SPAN * width_px / (tile_size * max(span_x, 1.0)))) + bias
