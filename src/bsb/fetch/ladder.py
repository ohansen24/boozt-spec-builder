"""Fetcher ladder: httpx -> playwright -> firecrawl (Phase 1).

Politeness defaults per build kit section 6.3: max 1 request per 2 seconds per
host, exponential backoff, per-host stop-loss, cache-first. No network code in
Phase 0.
"""


def fetch(url: str) -> str:
    raise NotImplementedError("Phase 1: fetcher ladder not implemented yet")
