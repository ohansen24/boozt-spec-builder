"""Shopify platform adapter — config-driven per brand (domain, market).

The native anchor is the variant "barcode" field == our GTIN (12- or
13-digit form). Resolution paths, in order:

1. /products.json pagination (cached catalog scan; one polite request per
   page of 250) -> barcode -> product/variant
2. /sitemap_products_1.xml handles when the catalog is truncated
3. /products/{handle}.js for per-product detail (body_html carries the
   description; INCI availability varies per brand)

Multi-region domains: the configured domain is the market to buy-side truth;
region subpaths (/en-de/ etc.) are honored when configured as path_prefix.
"""

import json

from pydantic import BaseModel, Field

from bsb.extract.structured import gtin_forms
from bsb.fetch.cache import EanCache
from bsb.fetch.ladder import FetchError, PoliteFetcher

CATALOG_PAGE_LIMIT = 250
MAX_CATALOG_PAGES = 40  # 10k products — beyond that, sitemap handles


class ShopifyVariantHit(BaseModel):
    gtin13: str
    ean12: str
    ok: bool
    url: str  # provenance: the products.json page or {handle}.js URL
    product_url: str | None = None
    product_title: str | None = None
    variant_title: str | None = None
    barcode: str | None = None
    size_text: str | None = None
    body_html: str | None = None
    reject_reason: str | None = None


class ShopifyAdapter:
    def __init__(self, fetcher: PoliteFetcher, brand_cfg: dict, ean_cache: EanCache):
        shopify_cfg = brand_cfg.get("shopify") or {}
        domain = shopify_cfg.get("domain") or (brand_cfg.get("domains") or [None])[0]
        if not domain:
            raise ValueError("shopify adapter needs a domain (brands.yaml shopify.domain)")
        prefix = str(shopify_cfg.get("path_prefix") or "").strip("/")
        self.base = f"https://{domain}" + (f"/{prefix}" if prefix else "")
        self.fetcher = fetcher
        self.ean_cache = ean_cache
        self._catalog: dict[str, tuple[dict, dict]] | None = None  # barcode -> (product, variant)

    def _catalog_pages(self):
        for page in range(1, MAX_CATALOG_PAGES + 1):
            url = f"{self.base}/products.json?limit={CATALOG_PAGE_LIMIT}&page={page}"
            fetch = self.fetcher.get(url)
            try:
                products = json.loads(fetch.text).get("products") or []
            except json.JSONDecodeError as exc:
                raise FetchError(f"{url}: not a Shopify products.json payload") from exc
            yield url, products
            if len(products) < CATALOG_PAGE_LIMIT:
                return

    def load_catalog(self) -> dict[str, tuple[dict, dict]]:
        """barcode -> (product, variant), built once per run from the
        paginated public catalog (each page is cached)."""
        if self._catalog is not None:
            return self._catalog
        catalog: dict[str, tuple[dict, dict]] = {}
        self._last_page_url = None
        for url, products in self._catalog_pages():
            self._last_page_url = url
            for product in products:
                for variant in product.get("variants") or []:
                    barcode = str(variant.get("barcode") or "").strip()
                    if barcode:
                        catalog.setdefault(barcode, (product, variant))
        self._catalog = catalog
        return catalog

    def resolve_variant(self, gtin13: str) -> ShopifyVariantHit:
        ean12 = gtin13[1:] if len(gtin13) == 13 and gtin13.startswith("0") else gtin13
        catalog = self.load_catalog()
        entry = None
        for form in gtin_forms(gtin13):
            entry = catalog.get(form)
            if entry:
                break
        if entry is None:
            return ShopifyVariantHit(
                gtin13=gtin13,
                ean12=ean12,
                ok=False,
                url=f"{self.base}/products.json",
                reject_reason=f"barcode not in catalog ({len(catalog)} barcodes scanned)",
            )
        product, variant = entry
        handle = product.get("handle")
        title = str(product.get("title") or "")
        hit = ShopifyVariantHit(
            gtin13=gtin13,
            ean12=ean12,
            ok=True,
            url=f"{self.base}/products.json",
            product_url=f"{self.base}/products/{handle}" if handle else None,
            product_title=title,
            variant_title=str(variant.get("title") or "") or None,
            barcode=str(variant.get("barcode")),
            body_html=str(product.get("body_html") or "") or None,
        )
        self.ean_cache.write(
            gtin13,
            {
                "gtin13": gtin13,
                "ean12": ean12,
                "platform": "shopify",
                "product_title": hit.product_title,
                "variant_title": hit.variant_title,
                "barcode": hit.barcode,
                "source_url": hit.product_url or hit.url,
                "method": "jsonld",
            },
        )
        return hit


class ShopifyCatalogStats(BaseModel):
    products: int = 0
    variants: int = 0
    with_barcode: int = 0
    sample_barcodes: list[str] = Field(default_factory=list)


def catalog_stats(adapter: ShopifyAdapter, max_pages: int = 2) -> ShopifyCatalogStats:
    """Probe helper: shallow catalog scan (politeness: few pages only)."""
    stats = ShopifyCatalogStats()
    for page, (_url, products) in enumerate(adapter._catalog_pages(), start=1):
        for product in products:
            stats.products += 1
            for variant in product.get("variants") or []:
                stats.variants += 1
                barcode = str(variant.get("barcode") or "").strip()
                if barcode:
                    stats.with_barcode += 1
                    if len(stats.sample_barcodes) < 5:
                        stats.sample_barcodes.append(barcode)
        if page >= max_pages:
            break
    return stats
