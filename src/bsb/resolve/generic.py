"""Generic resolver (kit 6.3 step 2): web search for "{gtin13}" and
"{ean12} {brand}", fetch candidate URLs, parse JSON-LD/microdata, and accept
only documents that assert the exact GTIN. Serves two roles:

- fallback PRIMARY source for brands without an EAN-addressable site
  (policy: green then requires TWO independent retailer families, never one)
- the engine behind the validator pool

A page without a GTIN assertion is WEAK support only and is returned with
gtin_anchored=False; it can never turn a field green.
"""

from urllib.parse import urlsplit

from pydantic import BaseModel

from bsb.extract.structured import page_asserts_gtin, parse_jsonld_products
from bsb.fetch.firecrawl import FirecrawlClient
from bsb.fetch.ladder import FetchError, PoliteFetcher

# hosts that never carry product-data value for us
_JUNK_HOSTS = (
    "amazon.",
    "ebay.",
    "aliexpress.",
    "youtube.",
    "facebook.",
    "instagram.",
    "pinterest.",
    "tiktok.",
    "reddit.",
    "barcodelookup",
    "upcitemdb",
    "ean-search",
)


class ResolverHit(BaseModel):
    url: str
    family: str  # registrable-ish host, the independence unit
    gtin_anchored: bool
    anchor_evidence: str | None = None
    name: str | None = None
    brand: str | None = None
    color: str | None = None
    size: str | None = None
    inci: str | None = None


def source_family(url: str) -> str:
    """Host-based family: narscosmetics.eu/.co.uk/.com are ONE family."""
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    parts = host.split(".")
    return parts[0] if parts else host


def _extract_product_fields(products: list[dict]) -> dict:
    for product in products:
        fields = {
            "name": product.get("name"),
            "brand": (
                product["brand"].get("name")
                if isinstance(product.get("brand"), dict)
                else product.get("brand")
            ),
            "color": product.get("color"),
            "size": product.get("size") or product.get("weight"),
        }
        if fields["name"]:
            return {k: (str(v) if v is not None else None) for k, v in fields.items()}
    return {}


class GenericResolver:
    def __init__(self, fetcher: PoliteFetcher, firecrawl: FirecrawlClient):
        self.fetcher = fetcher
        self.firecrawl = firecrawl

    @property
    def available(self) -> bool:
        return self.firecrawl.available

    def candidates(self, gtin13: str, ean12: str, brand: str, limit: int = 8) -> list[str]:
        urls: list[str] = []
        for query in (f'"{gtin13}"', f'"{ean12}" {brand}'):
            for item in self.firecrawl.search(query, limit=limit):
                url = str(item["url"])
                host = urlsplit(url).netloc.lower()
                if any(junk in host for junk in _JUNK_HOSTS):
                    continue
                if url not in urls:
                    urls.append(url)
        return urls

    def resolve(self, gtin13: str, ean12: str, brand: str, max_pages: int = 5) -> list[ResolverHit]:
        """Search + fetch + anchor-check. Hits are per source family (first
        anchored page per family wins)."""
        hits: list[ResolverHit] = []
        seen_families: set[str] = set()
        for url in self.candidates(gtin13, ean12, brand):
            family = source_family(url)
            if family in seen_families:
                continue
            if len(hits) >= max_pages:
                break
            try:
                page = self.fetcher.get(url)
            except FetchError:
                continue
            products = parse_jsonld_products(page.text)
            evidence = page_asserts_gtin(page.text, gtin13, products)
            fields = _extract_product_fields(products)
            hits.append(
                ResolverHit(
                    url=page.final_url,
                    family=family,
                    gtin_anchored=evidence is not None,
                    anchor_evidence=evidence,
                    **fields,
                )
            )
            seen_families.add(family)
        return hits
