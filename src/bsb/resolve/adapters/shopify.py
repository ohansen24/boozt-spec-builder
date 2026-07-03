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
    # variant option values keyed by their option NAME ("Size" -> "250ml"):
    # only a Color/Shade axis may ever ship as a color name
    variant_options: dict[str, str] = Field(default_factory=dict)
    barcode: str | None = None
    size_text: str | None = None
    body_html: str | None = None
    reject_reason: str | None = None


_SHADE_OPTIONS = {"color", "colour", "shade", "farbe"}
_SIZE_OPTIONS = {"size", "größe", "grösse", "volume"}


def variant_axes(product: dict, variant: dict) -> dict[str, str]:
    """Map option names to this variant's values, tolerating both the
    products.json shape (product.options[].name + variant.option1..3) and the
    {handle}.js shape (product.options as strings + variant.options list)."""
    names = []
    for opt in product.get("options") or []:
        names.append(str(opt.get("name")) if isinstance(opt, dict) else str(opt))
    values = []
    if isinstance(variant.get("options"), list):
        values = [str(v) for v in variant["options"]]
    else:
        values = [str(variant.get(f"option{i}")) for i in (1, 2, 3) if variant.get(f"option{i}")]
    return dict(zip(names, values, strict=False))


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

    def sitemap_handles(self, cap: int = 300) -> list[str]:
        """Published product handles from the product sitemap — the public
        catalog often contains unpublished multi-market handles that 404 on
        {handle}.js, while the sitemap lists what actually resolves."""
        import re as _re

        try:
            sm = self.fetcher.get(f"{self.base}/sitemap.xml")
        except FetchError:
            return []
        children = _re.findall(r"<loc>([^<]+sitemap_products[^<]*)</loc>", sm.text)
        handles: list[str] = []
        for child in children[:1]:  # main-locale product sitemap
            try:
                sm2 = self.fetcher.get(child.replace("&amp;", "&"))
            except FetchError:
                continue
            for url in _re.findall(r"<loc>([^<]+/products/([^<#?]+))</loc>", sm2.text):
                handle = url[1].strip("/")
                if handle not in handles:
                    handles.append(handle)
                if len(handles) >= cap:
                    break
        return handles

    def _js_index_lookup(self, forms: set[str]) -> tuple[dict, dict] | None:
        """Fallback: barcodes suppressed in products.json but present in the
        per-product {handle}.js (seen live: k18, olaplex). Builds a lazy
        barcode index over sitemap handles; every page is cached."""
        import json as _json

        if not hasattr(self, "_js_index"):
            self._js_index: dict[str, tuple[dict, dict]] = {}
            self._js_pending = self.sitemap_handles()
        while self._js_pending:
            handle = self._js_pending.pop(0)
            try:
                pjs = self.fetcher.get(f"{self.base}/products/{handle}.js")
                data = _json.loads(pjs.text)
            except (FetchError, _json.JSONDecodeError):
                continue
            product = {
                "handle": data.get("handle") or handle,
                "title": data.get("title"),
                "body_html": data.get("description"),
                "options": data.get("options") or [],
            }
            for variant in data.get("variants") or []:
                barcode = str(variant.get("barcode") or "").strip()
                if barcode:
                    self._js_index.setdefault(barcode, (product, variant))
            for form in forms:
                if form in self._js_index:
                    return self._js_index[form]
        for form in forms:
            if form in self._js_index:
                return self._js_index[form]
        return None

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
        forms = gtin_forms(gtin13)
        for form in forms:
            entry = catalog.get(form)
            if entry:
                break
        if entry is None:
            entry = self._js_index_lookup(forms)
        if entry is None:
            return ShopifyVariantHit(
                gtin13=gtin13,
                ean12=ean12,
                ok=False,
                url=f"{self.base}/products.json",
                reject_reason=(
                    f"barcode not in catalog ({len(catalog)} catalog barcodes, "
                    f"{len(getattr(self, '_js_index', {}))} handle.js barcodes scanned)"
                ),
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
            variant_options=variant_axes(product, variant),
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
