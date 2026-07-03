"""Validator pool (build kit 6.3 item 3), Phase 1 reality:

- lookfantastic (ENABLED): the only pool member reachable without an
  arms race. Search is JS-rendered (one Playwright render per master);
  the product page itself serves plain httpx and embeds `variationData`
  with a per-variant "barcode" — our exact ean12 form — plus the shade
  under choices[0].title. GTIN-anchor satisfied natively. One product
  page covers every shade of the master.
- incidecoder (ENABLED, WEAK): name-keyed search, no GTIN anywhere on
  product pages -> weak support per the kit: token-match notes only,
  can never turn a field green and never red on its own.
- douglas, boots, flaconi, sephora (DISABLED): 403 / challenge pages at
  both the httpx and Playwright rungs (Akamai/Imperva/Cloudflare).
  Revisit with Firecrawl (open question 5); we do not fight bot walls.

narscosmetics.eu/.co.uk/.com count as ONE source family; lookfantastic is
an independent family, so agreement upgrades to VERIFIED.
"""

import re
from datetime import datetime
from urllib.parse import quote_plus

from pydantic import BaseModel, Field

from bsb.extract.structured import extract_json_array, parse_jsonld_products
from bsb.fetch.cache import EanCache
from bsb.fetch.ladder import FetchError, PlaywrightSession, PoliteFetcher

LF_BASE = "https://www.lookfantastic.com"
_LF_JUNK_PATHS = ("/p/beauty-box/", "/p/customer-gift-voucher/")
_SIZE_IN_NAME = re.compile(r"(\d+(?:\.\d+)?)\s*(ml|g)\b", re.IGNORECASE)


class LfVariant(BaseModel):
    barcode: str  # ean12 form as LF publishes it
    shade: str | None = None
    variant_title: str | None = None
    swatch_hex: str | None = None  # future: color-code rule 3a


class LfProduct(BaseModel):
    url: str
    product_name: str | None = None
    size_text: str | None = None
    by_barcode: dict[str, LfVariant] = Field(default_factory=dict)
    from_cache: bool = False
    fetched_at: datetime | None = None


class WeakInci(BaseModel):
    url: str
    product_name: str | None = None
    inci_text: str | None = None


class LookfantasticValidator:
    """One rendered search + one httpx product fetch per master."""

    def __init__(self, fetcher: PoliteFetcher, playwright: PlaywrightSession):
        self.fetcher = fetcher
        self.playwright = playwright

    def _product_links(self, html: str) -> list[str]:
        links = []
        for href in re.findall(r'href="([^"]*/p/[^"]+)"', html):
            url = href if href.startswith("http") else LF_BASE + href
            if any(junk in url for junk in _LF_JUNK_PATHS):
                continue
            if url not in links:
                links.append(url)
        return links

    def find_product(self, ean12: str) -> LfProduct | None:
        """Search LF by barcode (rendered), then parse the first product page
        that actually contains that barcode in its variationData.

        LF A/B-buckets its search per session: some buckets match barcodes,
        others answer "no search results" for the same query. On an empty
        result, rotate to a fresh browser context (new bucket) and retry —
        a successful page overwrites any cached empty one."""
        search_url = f"{LF_BASE}/search/?q={quote_plus(ean12)}"
        links: list[str] = []
        for attempt in range(3):
            try:
                rendered = self.playwright.render(search_url, use_cache=attempt == 0)
            except FetchError:
                return None
            links = self._product_links(rendered.text)
            if links:
                break
            if "no search results" in rendered.text.lower():
                self.playwright.rotate_context()
                continue
            break

        for candidate in links[:2]:
            try:
                page = self.fetcher.get(candidate)
            except FetchError:
                continue
            product = self._parse_product(page.text, page.final_url, page.from_cache)
            if product is not None and ean12 in product.by_barcode:
                product.fetched_at = page.fetched_at
                return product
        return None

    def _parse_product(self, html: str, url: str, from_cache: bool) -> LfProduct | None:
        variation_data = extract_json_array(html, "const variationData = ")
        if variation_data is None:
            variation_data = extract_json_array(html, "variationData = ")
        if not variation_data:
            return None

        by_barcode: dict[str, LfVariant] = {}
        for entry in variation_data:
            if not isinstance(entry, dict):
                continue
            barcode = str(entry.get("barcode") or "")
            if not barcode:
                continue
            # only the "Shade" option axis is a shade — mascara variants use
            # optionKey "Size" ("Full Size"/"Travel Size"), which must never
            # ship as a color name
            shade_choice = next(
                (
                    c
                    for c in entry.get("choices") or []
                    if isinstance(c, dict) and c.get("optionKey") == "Shade"
                ),
                {},
            )
            by_barcode[barcode] = LfVariant(
                barcode=barcode,
                shade=shade_choice.get("title"),
                variant_title=entry.get("title"),
                swatch_hex=shade_choice.get("colour"),
            )

        name = None
        for product in parse_jsonld_products(html):
            name = product.get("name")
            if name:
                break
        size_match = _SIZE_IN_NAME.search(name or "")
        return LfProduct(
            url=url,
            product_name=name,
            size_text=f"{size_match.group(1)}{size_match.group(2).lower()}" if size_match else None,
            by_barcode=by_barcode,
            from_cache=from_cache,
        )


class IncidecoderWeak:
    """Weak-support INCI lookup: name search, first product hit, INCI text.
    No GTIN on the site — notes only."""

    def __init__(self, fetcher: PoliteFetcher):
        self.fetcher = fetcher

    def find_inci(self, brand: str, product_name: str) -> WeakInci | None:
        query = quote_plus(f"{brand} {product_name}".lower())
        try:
            search = self.fetcher.get(f"https://incidecoder.com/search?query={query}")
        except FetchError:
            return None
        m = re.search(r'href="(/products/[^"]+)"', search.text)
        if not m:
            return None
        url = "https://incidecoder.com" + m.group(1)
        try:
            page = self.fetcher.get(url)
        except FetchError:
            return None

        name_match = re.search(r"<title>(.*?)(?:\s*-\s*INCIDecoder)?</title>", page.text, re.DOTALL)
        ingredients = re.findall(r'href="/ingredients/[^"]+"[^>]*>([^<]+)</a>', page.text)
        inci = ", ".join(t.strip() for t in ingredients if t.strip()) or None
        return WeakInci(
            url=url,
            product_name=name_match.group(1).strip() if name_match else None,
            inci_text=inci,
        )


def cache_lf_hit(ean_cache: EanCache, gtin13: str, product: LfProduct, ean12: str) -> None:
    record = ean_cache.read(gtin13) or {"gtin13": gtin13, "ean12": ean12}
    variant = product.by_barcode.get(ean12)
    record["lookfantastic"] = {
        "url": product.url,
        "product_name": product.product_name,
        "shade": variant.shade if variant else None,
        "size_text": product.size_text,
        "barcode_matched": ean12,
    }
    ean_cache.write(gtin13, record)
