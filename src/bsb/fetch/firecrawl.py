"""Firecrawl client — fetch-ladder rung 3 and the generic resolver's search
backend (kit 6.3). Key-gated: activates when FIRECRAWL_API_KEY is present in
the environment or the repo .env; without it, `available` is False and
callers fall back or report the capability as blocked.

Only raw/rendered-HTML modes are used. If the LLM-extract mode is ever
enabled, its outputs are candidates subject to our own GTIN-anchor and
evidence-substring checks — never trusted directly (charter principle 3).
Politeness and cache rules apply to api.firecrawl.dev like any host.
"""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx

from bsb.fetch.cache import CachedFetch, HttpCache
from bsb.fetch.ladder import FetchError, HostRateLimiter

API_BASE = "https://api.firecrawl.dev/v1"
_API_HOST = "api.firecrawl.dev"


def load_env(repo_root: Path | None = None) -> None:
    """Populate os.environ from the repo .env (never overrides real env)."""
    root = repo_root or Path(__file__).resolve().parents[3]
    env_path = root / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class FirecrawlClient:
    def __init__(self, cache: HttpCache, limiter: HostRateLimiter, api_key: str | None = None):
        load_env()
        self.cache = cache
        self.limiter = limiter
        key = api_key or os.environ.get("FIRECRAWL_API_KEY")
        if key and not key.startswith("fc-"):
            key = "fc-" + key  # dashboard keys are fc-…; the prefix is easy to lose in copy
        self.api_key = key
        # usage accounting (the team credit endpoint rejects API keys, so we
        # count requests: scrape = 1 credit; search billing is per result —
        # we track both the calls and the results returned)
        self.usage = {"scrapes": 0, "searches": 0, "search_results": 0, "cache_hits": 0}

    def snapshot_usage(self) -> dict:
        return dict(self.usage)

    def usage_since(self, snapshot: dict) -> dict:
        return {k: self.usage[k] - snapshot.get(k, 0) for k in self.usage}

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _post(self, endpoint: str, payload: dict) -> dict:
        if not self.available:
            raise FetchError("Firecrawl unavailable: FIRECRAWL_API_KEY not set (.env)")
        self.limiter.wait(_API_HOST)
        response = httpx.post(
            f"{API_BASE}/{endpoint}",
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=90,
        )
        if response.status_code != 200:
            raise FetchError(
                f"firecrawl {endpoint}: HTTP {response.status_code} {response.text[:160]}"
            )
        return response.json()

    def scrape(self, url: str, render: bool = True, use_cache: bool = True) -> CachedFetch:
        """Rung 3 fetch. render=True waits for JS; raw HTML either way."""
        cache_key = f"firecrawl:{url}"
        if use_cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                self.usage["cache_hits"] += 1
                return cached
        self.usage["scrapes"] += 1
        data = self._post(
            "scrape",
            {"url": url, "formats": ["rawHtml"], "waitFor": 3000 if render else 0},
        )
        html = ((data.get("data") or {}).get("rawHtml")) or ""
        if not html:
            raise FetchError(f"firecrawl scrape returned no HTML for {url}")
        return self.cache.put(
            CachedFetch(
                url=cache_key,
                final_url=str((data.get("data") or {}).get("metadata", {}).get("url") or url),
                status=200,
                content_type="text/html;firecrawl",
                text=html,
                fetched_at=datetime.now(UTC),
                via="firecrawl",
            )
        )

    def search(self, query: str, limit: int = 8, use_cache: bool = True) -> list[dict]:
        """Search backend for the generic resolver: [{url, title}]. Results
        are candidates only — every page still has to pass the GTIN anchor."""
        digest = hashlib.sha256(query.encode()).hexdigest()[:24]
        cache_key = f"firecrawl-search:{digest}:{query[:80]}"
        if use_cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                self.usage["cache_hits"] += 1
                return json.loads(cached.text)
        self.usage["searches"] += 1
        data = self._post("search", {"query": query, "limit": limit})
        raw = data.get("data") or []
        if isinstance(raw, dict):  # v1 sometimes nests under data.web
            raw = raw.get("web") or []
        results = [
            {"url": item.get("url"), "title": item.get("title")}
            for item in raw
            if isinstance(item, dict) and item.get("url")
        ]
        self.usage["search_results"] += len(results)
        self.cache.put(
            CachedFetch(
                url=cache_key,
                final_url=cache_key,
                status=200,
                content_type="application/json",
                text=json.dumps(results),
                fetched_at=datetime.now(UTC),
                via="firecrawl",
            )
        )
        return results
