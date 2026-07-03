"""Wayback Machine fallback (last rung of the source ladder).

When a product is gone from the current brand site in every region
(410/404/stateless), archived snapshots of the brand's own PDP for that GTIN
become a valid fallback source. The GTIN-anchor rule is unchanged — the
snapshot's product state must anchor the requested GTIN. Archive-sourced
fields ship yellow, never green, and provenance records the snapshot date.
web.archive.org gets the standard politeness rules via the shared fetcher.
"""

import json
import re

from pydantic import BaseModel

from bsb.fetch.cache import CachedFetch
from bsb.fetch.ladder import FetchError, PoliteFetcher

CDX_URL = "https://web.archive.org/cdx/search/cdx"


class Snapshot(BaseModel):
    url: str  # id_ form: original page bytes, no Wayback toolbar
    original_url: str
    timestamp: str  # YYYYMMDDhhmmss

    @property
    def date(self) -> str:
        ts = self.timestamp
        return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"


class WaybackArchive:
    def __init__(self, fetcher: PoliteFetcher, domain: str):
        self.fetcher = fetcher
        self.domain = domain

    def find_pdp_snapshot(self, gtin13: str) -> Snapshot | None:
        """Latest archived 200 snapshot of the brand's own PDP for this GTIN
        (PDPs are addressed /en/{slug}/{gtin13}.html — slug wildcarded via a
        CDX original-URL filter; image/static hits are excluded)."""
        query = (
            f"{CDX_URL}?url={self.domain}&matchType=domain"
            f"&filter=original:.*/en/.*{re.escape(gtin13)}\\.html.*"
            "&filter=statuscode:200&output=json&collapse=digest&limit=50"
        )
        try:
            fetch: CachedFetch = self.fetcher.get(query)
        except FetchError:
            return None
        try:
            rows = json.loads(fetch.text) if fetch.text.strip() else []
        except json.JSONDecodeError:
            return None
        if len(rows) < 2:
            return None

        # rows[0] is the header: urlkey timestamp original mimetype status …
        latest = max(rows[1:], key=lambda r: r[1])
        timestamp, original = latest[1], latest[2]
        return Snapshot(
            url=f"https://web.archive.org/web/{timestamp}id_/{original}",
            original_url=original,
            timestamp=timestamp,
        )
