"""bsb probe-brand: probe before building (Phase 2, Stage 2).

Detects the platform, tests EAN addressability with real EANs where we have
them (or the site's own barcodes where we don't), checks on-site INCI
availability, and records fixtures. Output: a probe report JSON + a draft
brands.yaml entry. No gates, no bespoke code — anything beyond
Shopify/SFCC/generic waits until a probe proves the need AND an order exists.
"""

import gzip
import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

from bsb.extract.structured import (
    page_asserts_gtin,
    parse_jsonld_products,
    parse_sfcc_product_state,
)
from bsb.fetch.ladder import FetchError, PoliteFetcher

_SFCC_SITE = re.compile(r"/on/demandware\.store/Sites-([A-Za-z0-9_]+)-Site/([A-Za-z_]+)/")
_INCI_TOKENS = re.compile(
    r"\b(AQUA|WATER|GLYCERIN|PARFUM|DIMETHICONE|PHENOXYETHANOL|TOCOPHEROL|LINALOOL)\b",
    re.IGNORECASE,
)


class ProbeReport(BaseModel):
    brand: str
    domain: str | None = None
    platform: str = "unknown"  # shopify | sfcc | unknown
    reachable: bool = False
    ean_addressable: str = "untested"  # yes | partial | no | untested
    ean_evidence: str = ""
    samples_tested: int = 0
    samples_hit: int = 0
    barcodes_in_catalog: str = ""  # shopify: coverage note
    inci_on_site: str = "untested"  # yes | no | untested
    inci_evidence: str = ""
    sfcc_controller_base: str | None = None
    notes: list[str] = Field(default_factory=list)
    fixtures: list[str] = Field(default_factory=list)

    def draft_yaml(self) -> str:
        lines = [f"{self.brand}:"]
        lines.append(f"  # probe {self.platform}; ean_addressable={self.ean_addressable}")
        if self.platform == "shopify" and self.domain:
            lines.append("  adapter: shopify")
            lines.append("  shopify:")
            lines.append(f"    domain: {self.domain}")
        elif self.platform == "sfcc" and self.sfcc_controller_base:
            lines.append("  adapter: sfcc")
            lines.append(f"  controller_base: {self.sfcc_controller_base}")
        else:
            lines.append("  adapter: generic # no platform match — retailer-primary policy")
        return "\n".join(lines)


def _save_fixture(out_dir: Path, name: str, text: str) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.html.gz"
    path.write_bytes(gzip.compress(text.encode("utf-8"), 9))
    return str(path)


def _detect_platform(fetcher: PoliteFetcher, domain: str, report: ProbeReport) -> str | None:
    """Returns the homepage HTML when reachable; sets platform fields."""
    try:
        home = fetcher.get(f"https://{domain}/")
    except FetchError as exc:
        report.notes.append(f"homepage unreachable: {exc}")
        return None
    report.reachable = True

    match = _SFCC_SITE.search(home.text)
    if match or "demandware.static" in home.text:
        report.platform = "sfcc"
        if match:
            report.sfcc_controller_base = f"https://{domain}/on/demandware.store/Sites-{match.group(1)}-Site/{match.group(2)}/"
        return home.text

    try:
        pj = fetcher.get(f"https://{domain}/products.json?limit=1")
        if json.loads(pj.text).get("products") is not None:
            report.platform = "shopify"
            return home.text
    except (FetchError, json.JSONDecodeError):
        pass
    if "cdn.shopify.com" in home.text or "Shopify.theme" in home.text:
        report.platform = "shopify"
        return home.text

    report.platform = "unknown"
    return home.text


def _inci_check(html: str, report: ProbeReport) -> None:
    hits = _INCI_TOKENS.findall(html)
    if len(hits) >= 3:
        report.inci_on_site = "yes"
        report.inci_evidence = f"{len(hits)} INCI-ish tokens on sampled product page"
    else:
        report.inci_on_site = "no"
        report.inci_evidence = f"only {len(hits)} INCI-ish tokens on sampled product page"


def _probe_shopify(
    fetcher: PoliteFetcher, domain: str, samples: list[str], report: ProbeReport, fx: Path
):
    from bsb.fetch.cache import EanCache
    from bsb.resolve.adapters.shopify import ShopifyAdapter, catalog_stats

    adapter = ShopifyAdapter(fetcher, {"shopify": {"domain": domain}}, EanCache(fx / "cache"))
    stats = catalog_stats(adapter, max_pages=2)
    pct = (stats.with_barcode / stats.variants * 100) if stats.variants else 0.0
    report.barcodes_in_catalog = (
        f"{stats.with_barcode}/{stats.variants} variants carry barcodes "
        f"({pct:.0f}%, first 2 catalog pages)"
    )
    try:
        page = fetcher.get(f"https://{domain}/products.json?limit={250}&page=1")
        report.fixtures.append(_save_fixture(fx, "products_page1", page.text))
    except FetchError:
        pass

    if samples:
        adapter._catalog = None  # full scan for real-EAN addressability
        hits = 0
        first_hit = None
        for gtin in samples:
            hit = adapter.resolve_variant(gtin if len(gtin) == 13 else "0" + gtin)
            if hit.ok:
                hits += 1
                first_hit = first_hit or hit
        report.samples_tested, report.samples_hit = len(samples), hits
        report.ean_addressable = "yes" if hits == len(samples) else "partial" if hits else "no"
        report.ean_evidence = f"{hits}/{len(samples)} sample EANs matched variant barcodes"
        if first_hit and first_hit.body_html:
            _inci_check(first_hit.body_html, report)
        if report.inci_on_site == "untested" and first_hit and first_hit.product_url:
            try:
                pdp = fetcher.get(first_hit.product_url)
                _inci_check(pdp.text, report)
                report.fixtures.append(_save_fixture(fx, "sample_pdp", pdp.text))
            except FetchError:
                pass
    else:
        report.ean_addressable = "yes" if stats.with_barcode else "no"
        report.ean_evidence = "self-test: site's own variant barcodes are the anchor"
        if stats.sample_barcodes:
            report.notes.append(f"sample catalog barcodes: {stats.sample_barcodes[:3]}")
        # INCI from the first product page
        try:
            page = fetcher.get(f"https://{domain}/products.json?limit=5&page=1")
            products = json.loads(page.text).get("products") or []
            if products:
                _inci_check(str(products[0].get("body_html") or ""), report)
                if report.inci_on_site == "no" and products[0].get("handle"):
                    pdp = fetcher.get(f"https://{domain}/products/{products[0]['handle']}")
                    _inci_check(pdp.text, report)
                    report.fixtures.append(_save_fixture(fx, "sample_pdp", pdp.text))
        except (FetchError, json.JSONDecodeError):
            pass


def _probe_sfcc(fetcher: PoliteFetcher, samples: list[str], report: ProbeReport, fx: Path):
    if not report.sfcc_controller_base:
        report.notes.append("SFCC detected but no controller base found on homepage")
        return
    if not samples:
        report.ean_addressable = "untested"
        report.notes.append("no sample EANs — Product-Show addressability untested")
        return
    hits = 0
    for gtin in samples:
        gtin13 = gtin if len(gtin) == 13 else "0" + gtin
        url = f"{report.sfcc_controller_base}Product-Show?pid={gtin13}"
        try:
            page = fetcher.get(url)
        except FetchError:
            continue
        state = parse_sfcc_product_state(page.text)
        if state and str(state.get("ID")) == gtin13:
            hits += 1
            if hits == 1:
                _inci_check(page.text, report)
                report.fixtures.append(_save_fixture(fx, "sample_pdp", page.text))
    report.samples_tested, report.samples_hit = len(samples), hits
    report.ean_addressable = "yes" if hits == len(samples) else "partial" if hits else "no"
    report.ean_evidence = f"{hits}/{len(samples)} sample EANs self-anchored via Product-Show"


def _probe_unknown(
    fetcher: PoliteFetcher, domain: str, samples: list[str], report: ProbeReport, fx: Path
):
    """No platform match: JSON-LD GTIN presence on a sitemap-sampled page."""
    product_url = None
    for sitemap in (f"https://{domain}/sitemap.xml", f"https://{domain}/sitemap_index.xml"):
        try:
            sm = fetcher.get(sitemap)
        except FetchError:
            continue
        locs = re.findall(r"<loc>([^<]+)</loc>", sm.text)
        product_locs = [u for u in locs if re.search(r"/(product|produkt|p)/|\.html$", u)]
        nested = [u for u in locs if u.endswith(".xml") and "product" in u]
        if not product_locs and nested:
            try:
                sm2 = fetcher.get(nested[0])
                product_locs = re.findall(r"<loc>([^<]+)</loc>", sm2.text)[:5]
            except FetchError:
                pass
        if product_locs:
            product_url = product_locs[0]
            break
    if not product_url:
        report.notes.append("no product URL found via sitemap")
        return
    try:
        pdp = fetcher.get(product_url)
    except FetchError as exc:
        report.notes.append(f"sample product page unreachable: {exc}")
        return
    report.fixtures.append(_save_fixture(fx, "sample_pdp", pdp.text))
    products = parse_jsonld_products(pdp.text)
    gtin_keys = [
        key
        for product in products
        for key in ("gtin13", "gtin12", "gtin", "ean")
        if product.get(key)
    ]
    if gtin_keys:
        report.ean_evidence = (
            f"JSON-LD carries {sorted(set(gtin_keys))} on sampled PDP ({product_url})"
        )
        report.ean_addressable = "partial"
    else:
        report.ean_evidence = f"no GTIN in JSON-LD on sampled PDP ({product_url})"
        report.ean_addressable = "no"
    _inci_check(pdp.text, report)

    if samples:
        hits = 0
        for gtin in samples[:3]:
            gtin13 = gtin if len(gtin) == 13 else "0" + gtin
            if page_asserts_gtin(pdp.text, gtin13, products):
                hits += 1
        if hits:
            report.notes.append(f"{hits} sample EAN(s) asserted on the sampled page")


def probe_brand(
    brand_key: str,
    brand_cfg: dict,
    fetcher: PoliteFetcher,
    samples: list[str],
    fixtures_root: Path,
) -> ProbeReport:
    report = ProbeReport(brand=brand_key)
    if brand_cfg.get("out_of_scope"):
        report.notes.append("out_of_scope in brands.yaml — not probed")
        return report

    fx = fixtures_root / brand_key
    for domain in brand_cfg.get("domains") or []:
        report.domain = domain
        html = _detect_platform(fetcher, domain, report)
        if html is None:
            continue
        report.fixtures.append(_save_fixture(fx, "homepage", html))
        if report.platform == "shopify":
            _probe_shopify(fetcher, domain, samples, report, fx)
        elif report.platform == "sfcc":
            _probe_sfcc(fetcher, samples, report, fx)
        else:
            _probe_unknown(fetcher, domain, samples, report, fx)
        break
    return report
