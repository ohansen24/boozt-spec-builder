"""SFCC catalog-index adapter (build kit 6.x): barcode-is-not-pid storefronts.

Unlike NARS — where ``Product-Show?pid={gtin13}`` 301s straight to the variant
PDP because the barcode IS the product id — Benefit's storefront keys variants
by an internal ``page_id_variant`` (e.g. ``FM188``), never the barcode. The
barcode->variant pairing exists only inside the master PDP's analytics
datalayer::

    "variants":[{"name":..,"image_url":..,"upc":"602004111548",
                 "page_id_variant":"FM188"}, ...]

So resolution is a two-stage catalog index:

1. ``build_index``: crawl the catalog product sitemap (~105 PDPs), pull each
   PDP's ``variants[]`` datalayer, and build ``{upc -> CatalogEntry}`` where
   the entry carries the master code, the master PDP url (provenance +
   referer), the variant code, and the product name. This pairing is
   first-party and GTIN-anchored: the barcode is emitted right next to its
   variant code on the brand's own PDP.

2. ``resolve_variant``: ``Product-Variation?pid={variant_code}`` returns the
   SELECTED variant's product-state JSON. ``variationAttributes`` gives the
   canonical size and color (shade) ``displayValue``s
   ("1-Amaze 'Em (Fair Neutral)"). The returned ``product.id`` MUST equal the
   requested variant code (anchor rule, charter principle 2) or the result is
   rejected.

INCI is sparse on-site (covered by the validator-of-last-resort). Color codes
are proprietary shade names with no Boozt lexicon, so they fail closed to
Felina. Everything storefront-specific (controller base, catalog sitemap)
lives in brands.yaml.
"""

import json
import re
from urllib.parse import quote

from pydantic import BaseModel

from bsb.fetch.cache import CachedFetch, EanCache
from bsb.fetch.ladder import FetchError, PlaywrightSession, PoliteFetcher
from bsb.resolve.adapters.sfcc import VariantResult

# master code suffix on a canonical PDP url: .../product/<slug>-<CODE>.html
_MASTER_CODE = re.compile(r"-([A-Z0-9]+)\.html(?:$|[?#])")
# hex embedded in some (not all) analytics image urls: .../Large_f6dece_1_...jpg
_IMG_HEX = re.compile(r"Large_([0-9a-fA-F]{6})_")


def _product_category(dec: str, master_code: str) -> tuple[str | None, str | None]:
    """The product's own (category, categoryID) from the GTM datalayer entry
    keyed by its master id: ``{"id":"BOIINGHC","name":..,"category":
    "Concealer","categoryID":"concealer",..}``. This is Benefit's first-party
    classification (GTIN-anchored to the master) — distinct from the nav-menu
    "you may also like" entries for other products on the same page. Returns
    (None, None) when the datalayer entry is absent (non-product SKUs)."""
    match = re.search(
        r'"id"\s*:\s*"' + re.escape(master_code) + r'"\s*,\s*"name"\s*:\s*"[^"]*"\s*'
        r',\s*"category"\s*:\s*"([^"]+)"\s*,\s*"categoryID"\s*:\s*"([^"]+)"',
        dec,
    )
    if match:
        return match.group(1), match.group(2)
    return None, None


class CatalogEntry(BaseModel):
    upc: str
    gtin13: str
    master_code: str
    master_pdp_url: str
    variant_code: str
    product_name: str
    hex: str | None = None
    site_category: str | None = None  # Benefit's own label, e.g. "Concealer"
    site_category_id: str | None = None  # datalayer slug, e.g. "concealer"


class SfccCatalogAdapter:
    def __init__(
        self,
        fetcher: PoliteFetcher,
        brand_cfg: dict,
        ean_cache: EanCache,
        playwright: PlaywrightSession | None = None,
    ):
        self.fetcher = fetcher
        self.brand_cfg = brand_cfg
        self.controller_base = str(brand_cfg["controller_base"]).rstrip("/") + "/"
        self.catalog_sitemap = str(brand_cfg["catalog_sitemap"])
        self.ean_cache = ean_cache
        self.playwright = playwright
        self._index: dict[str, CatalogEntry] | None = None
        # regional sibling storefronts on the same SFCC platform, tried for a
        # variant whose shade the primary site no longer lists (discontinued).
        # Same manufacturer -> brand authority, better than retailer-primary.
        self.family_controllers = [
            {
                "market": str(c.get("market") or "?"),
                "base": str(c["controller_base"]).rstrip("/") + "/",
            }
            for c in (brand_cfg.get("brand_family_controllers") or [])
        ]

    # ---- stage 1: catalog crawl -> upc index ---------------------------------

    def catalog_pdp_urls(self) -> list[str]:
        """The catalog's product PDPs, from the SFRA product sitemap."""
        fetch = self.fetcher.get(self.catalog_sitemap)
        locs = re.findall(r"<loc>([^<]+)</loc>", fetch.text)
        return [loc for loc in locs if "/product/" in loc]

    def extract_variants(self, html: str, pdp_url: str) -> list[CatalogEntry]:
        """Parse the analytics ``variants[]`` datalayer off one PDP. Entries
        without a upc or a variant code are skipped (defensive — every real
        Benefit variant carries both)."""
        master_match = _MASTER_CODE.search(pdp_url)
        master_code = master_match.group(1) if master_match else ""
        dec = html.replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&")
        match = re.search(r'"variants"\s*:\s*\[', dec)
        if not match:
            return []
        start = match.end() - 1
        depth = 0
        end = None
        for i in range(start, len(dec)):
            char = dec[i]
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            return []
        try:
            arr = json.loads(dec[start:end])
        except json.JSONDecodeError:
            return []

        out: list[CatalogEntry] = []
        for var in arr:
            upc = str(var.get("upc") or "").strip()
            variant_code = str(var.get("page_id_variant") or "").strip()
            if not upc or not variant_code:
                continue
            gtin13 = upc if len(upc) == 13 else ("0" + upc if len(upc) == 12 else upc)
            hex_match = _IMG_HEX.search(str(var.get("image_url") or ""))
            out.append(
                CatalogEntry(
                    upc=upc,
                    gtin13=gtin13,
                    master_code=master_code,
                    master_pdp_url=pdp_url,
                    variant_code=variant_code,
                    product_name=str(var.get("name") or "").strip(),
                    hex=(hex_match.group(1).upper() if hex_match else None),
                )
            )
        return out

    def build_index(self, progress=lambda _msg: None) -> dict[str, CatalogEntry]:
        """Crawl every catalog PDP once, keyed by upc. Cache-first (each PDP
        fetch is cached), so a re-run is instant and idempotent. Later PDPs
        never clobber an earlier upc — a barcode belongs to exactly one master
        PDP, and a duplicate would be a catalog artefact, not a correction."""
        if self._index is not None:
            return self._index
        index: dict[str, CatalogEntry] = {}
        pdps = self.catalog_pdp_urls()
        tick = progress
        done = 0
        for url in pdps:
            done += 1
            try:
                fetch = self.fetcher.get(url)
            except FetchError as exc:
                tick(f"  ✗ catalog PDP {url}: {exc}")
                continue
            entries = self.extract_variants(fetch.text, url)
            if entries:
                dec = fetch.text.replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&")
                site_cat, site_cid = _product_category(dec, entries[0].master_code)
                for entry in entries:
                    entry.site_category = site_cat
                    entry.site_category_id = site_cid
            for entry in entries:
                index.setdefault(entry.upc, entry)
            tick(f"index {done}/{len(pdps)}: {url.split('/')[-1]} (+{len(entries)} variants)")
        self._index = index
        return index

    # ---- stage 2: GTIN-anchored variant resolution ---------------------------

    def _controller(self, name: str, **params: object) -> str:
        query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
        return f"{self.controller_base}{name}?{query}"

    def _get(self, url: str, *, referer: str, ajax: bool) -> CachedFetch:
        return self.fetcher.get(url, referer=referer, ajax=ajax)

    @staticmethod
    def _attr(product: dict, attr_id: str) -> dict | None:
        for attr in product.get("variationAttributes") or []:
            if attr.get("id") == attr_id:
                return attr
        return None

    @classmethod
    def _selected(cls, product: dict, attr_id: str) -> str | None:
        attr = cls._attr(product, attr_id)
        if attr is None:
            return None
        for val in attr.get("values") or []:
            if val.get("selected"):
                dv = str(val.get("displayValue") or "").strip()
                return dv or None
        return None

    @classmethod
    def _selected_hex(cls, product: dict) -> str | None:
        """The selected color value's `value` — a hex on Benefit's swatch axes
        (e.g. 'F6DECE'). Feeds the Stage 2 swatch-hex color-code proposer."""
        attr = cls._attr(product, "color")
        if attr is None:
            return None
        for val in attr.get("values") or []:
            if val.get("selected"):
                v = str(val.get("value") or "").strip()
                return v if re.fullmatch(r"[0-9a-fA-F]{6}", v) else None
        return None

    @classmethod
    def _shade(cls, product: dict, hex_hint: str | None) -> tuple[str | None, bool, bool]:
        """Resolve the shade for a variant. Returns
        (shade, has_color_axis, unresolved). ``Product-Variation?pid=variant``
        usually pre-selects the color, but for some variants (notably
        discontinued shades no longer in the master swatch list) nothing is
        selected — then we try the variant's own image hex against the value
        list. A color axis with no resolvable shade is `unresolved` (fail
        closed downstream), never treated as colorless."""
        attr = cls._attr(product, "color")
        if attr is None:
            return None, False, False  # simple/colorless product
        selected = cls._selected(product, "color")
        if selected:
            return selected, True, False
        if hex_hint:
            want = hex_hint.upper()
            for val in attr.get("values") or []:
                if str(val.get("value") or "").upper() == want:
                    dv = str(val.get("displayValue") or "").strip()
                    if dv:
                        return dv, True, False
        return None, True, True  # color axis present but shade not recoverable

    def _family_shade(self, variant_code: str, referer: str) -> tuple[str | None, str | None]:
        """A discontinued shade the primary site dropped may still be listed on
        a regional sibling storefront. Retry Product-Variation for the SAME
        variant code on each family controller; return (shade, market) from the
        first that anchors (product.id == variant_code) with a selected color."""
        for ctrl in self.family_controllers:
            url = f"{ctrl['base']}Product-Variation?pid={quote(variant_code, safe='')}&quantity=1"
            try:
                fetch = self._get(url, referer=referer, ajax=True)
                product = (json.loads(fetch.text) or {}).get("product") or {}
            except (FetchError, json.JSONDecodeError):
                continue
            if str(product.get("id") or "") != variant_code:
                continue
            shade = self._selected(product, "color")
            if shade:
                return shade, ctrl["market"]
        return None, None

    def resolve_variant(self, entry: CatalogEntry) -> VariantResult:
        """Product-Variation keyed by the variant code returns that exact
        variant's product-state; the selected color/size displayValues are the
        canonical shade and size. Anchor rule: returned product.id must equal
        the requested variant code."""
        url = self._controller("Product-Variation", pid=entry.variant_code, quantity=1)
        result = VariantResult(
            gtin13=entry.gtin13,
            ean12=entry.upc if len(entry.upc) == 12 else entry.gtin13[1:],
            ok=False,
            master_id=entry.master_code,
            url=url,
        )
        try:
            fetch = self._get(url, referer=entry.master_pdp_url, ajax=True)
        except FetchError as exc:
            result.reject_reason = str(exc)
            return result
        result.fetched_at = fetch.fetched_at
        result.from_cache = fetch.from_cache
        result.via = fetch.via

        try:
            product = (json.loads(fetch.text) or {}).get("product") or {}
        except json.JSONDecodeError:
            result.reject_reason = "Product-Variation returned non-JSON payload"
            return result

        returned_id = str(product.get("id") or "")
        result.returned_id = returned_id
        if returned_id != entry.variant_code:
            result.reject_reason = (
                f"variation partial returned id {returned_id!r}, requested {entry.variant_code!r}"
            )
            return result

        shade, _has_color_axis, unresolved = self._shade(product, entry.hex)
        size_text = self._selected(product, "size")
        product_name = str(product.get("productName") or entry.product_name or "").strip()

        family_market = None
        if unresolved and self.family_controllers:
            fam_shade, family_market = self._family_shade(entry.variant_code, entry.master_pdp_url)
            if fam_shade:
                shade, unresolved = fam_shade, False

        result.ok = True
        result.product_name = product_name
        result.shade = shade
        result.size_text = size_text
        result.shade_unresolved = unresolved
        result.swatch_hex = self._selected_hex(product) or entry.hex  # Stage 2 signal
        if family_market and shade:
            result.snippet = (
                f'"id":"{returned_id}" (upc {entry.upc}) — shade off primary site (discontinued); '
                f"recovered from {family_market} brand-family sibling: color {shade!r}"
            )
        elif unresolved:
            result.snippet = (
                f'"id":"{returned_id}" (upc {entry.upc}) — color axis present but shade not in '
                f"current swatch list (hex {entry.hex}); likely a discontinued shade"
            )
        else:
            result.snippet = (
                f'"id":"{returned_id}" (upc {entry.upc} @ master PDP) '
                f"… color {shade!r} size {size_text!r}"
            )

        self.ean_cache.write(
            entry.gtin13,
            {
                "gtin13": entry.gtin13,
                "ean12": result.ean12,
                "master_id": entry.master_code,
                "product_name": product_name,
                "shade": shade,
                "size_text": size_text,
                "source_url": url,
                "pdp_url": entry.master_pdp_url,
                "method": "sfcc_catalog",
                "via": result.via,
                "fetched_at": (
                    fetch.fetched_at.isoformat(timespec="seconds") if fetch.fetched_at else None
                ),
            },
        )
        return result
