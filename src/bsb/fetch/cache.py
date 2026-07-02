"""Disk caches (build kit principle 6: idempotent and cached).

- HttpCache: raw fetch payloads keyed by URL hash under cache/http/.
  Re-runs fetch only misses; every run is reproducible from cache.
- EanCache: resolved per-GTIN records under cache/eans/{gtin13}.json
  (the layout named in build kit section 4).
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel


class CachedFetch(BaseModel):
    url: str
    final_url: str  # after redirects (Product-Show 301s to the canonical PDP)
    status: int
    content_type: str = ""
    text: str
    fetched_at: datetime
    from_cache: bool = False
    via: str = "httpx"  # httpx | playwright


class HttpCache:
    def __init__(self, root: Path):
        self.dir = Path(root) / "http"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, url: str) -> Path:
        return self.dir / (hashlib.sha256(url.encode()).hexdigest() + ".json")

    def get(self, url: str) -> CachedFetch | None:
        path = self._path(url)
        if not path.exists():
            return None
        cached = CachedFetch.model_validate_json(path.read_text(encoding="utf-8"))
        cached.from_cache = True
        return cached

    def put(self, fetch: CachedFetch) -> CachedFetch:
        self._path(fetch.url).write_text(fetch.model_dump_json(), encoding="utf-8")
        return fetch


class EanCache:
    def __init__(self, root: Path):
        self.dir = Path(root) / "eans"
        self.dir.mkdir(parents=True, exist_ok=True)

    def read(self, gtin13: str) -> dict | None:
        path = self.dir / f"{gtin13}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, gtin13: str, record: dict) -> None:
        record = dict(record)
        record.setdefault("cached_at", datetime.now(UTC).isoformat(timespec="seconds"))
        (self.dir / f"{gtin13}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8"
        )
