"""NARS SFCC-SFRA adapter (build kit 6.3, live-captured architecture).

Master-first flow:
1. discover_master: one variant PDP per base-name group, reached through the
   Product-Show?pid={gtin13} controller (301s to the canonical PDP — never
   guess slugs: ODM base names differ from site names, e.g. ODM "Talc-Free
   Blush" is "POWDER BLUSH" on-site). Parses the product-state object for the
   master pid and full swatch list, plus size and the INCI accordion.
2. resolve_variant: Product-Variation?pid={master}&dwvar_{master}_color=
   {colorValId}&Quantity=1&format=ajax per EAN. The color val id equals the
   GTIN-13 on some masters and is an internal shade code on others (both
   foundations) — mapped per variant via the master swatch data. The returned
   partial's product-state "ID" must equal the requested gtin13 or the result
   is rejected (GTIN-anchor rule, charter principle 2).

Escalation: plain cookie-less httpx first; on a bot-shell response the
adapter switches to the Playwright rung (consent accepted once, context kept
alive) for the rest of the run. Every payload is cached; resolved variants
land in cache/eans/{gtin13}.json with full-URL provenance.
"""

import re
from datetime import datetime
from urllib.parse import quote

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from bsb.extract.structured import (
    jsonld_selected_shade,
    parse_jsonld_products,
    parse_sfcc_product_state,
)
from bsb.fetch.cache import CachedFetch, EanCache
from bsb.fetch.ladder import BotShell, FetchError, PlaywrightSession, PoliteFetcher

_SIZE_DIV = re.compile(
    r'class="attribute single-size">\s*<div class="value">\s*<span>([^<]+)</span>', re.DOTALL
)


class MasterResult(BaseModel):
    master_id: str
    product_name: str
    pdp_url: str  # final canonical URL (provenance)
    discovered_via_gtin: str
    selected_id: str  # product-state "ID": the variant the PDP had selected
    selected_shade: str | None = None
    shade_by_gtin: dict[str, str] = Field(default_factory=dict)  # empty = simple product
    # gtin13 -> dwvar color value id. On some masters (Powder Blush) the val id
    # IS the gtin13; on others (both foundations) it is an internal shade code
    # ('4251070360' for Oslo) — joined via shade name from the swatch list.
    color_val_by_gtin: dict[str, str] = Field(default_factory=dict)
    size_text: str | None = None
    inci_text: str | None = None
    inci_selected_gtin: str | None = None  # which shade the PDP had selected
    region: str = "EU"
    fallback_note: str | None = None  # why the EU site could not serve this
    fetched_at: datetime | None = None
    from_cache: bool = False

    @property
    def is_simple_product(self) -> bool:
        return not self.shade_by_gtin


class VariantResult(BaseModel):
    gtin13: str
    ean12: str
    ok: bool
    master_id: str
    url: str  # full Product-Variation URL (provenance)
    product_name: str | None = None
    shade: str | None = None
    size_text: str | None = None
    returned_id: str | None = None
    reject_reason: str | None = None
    snippet: str = ""
    fetched_at: datetime | None = None
    from_cache: bool = False
    via: str = "httpx"


def _has_product_state(text: str) -> bool:
    return parse_sfcc_product_state(text) is not None


_MIN_INCI_DOTS = 3
_INCI_SEPARATORS = (" · ", " • ")  # NARS mixes middle dots and bullets per PDP
_INCI_LABEL = re.compile(r"^[A-Z][A-Z /-]*INGREDIENTS?\s*:\s*")


def _inci_separator_count(text: str) -> int:
    return max(text.count(sep) for sep in _INCI_SEPARATORS)


def extract_inci(html: str) -> str | None:
    """The INCI list from the INGREDIENTS accordion. Accordions mix marketing
    copy ("KEY INGREDIENTS: ... Helps soothe ...") and a disclaimer paragraph
    with the real list; the INCI segment is identified as the block with the
    most separator hits — NARS publishes " · "-separated INCI on some PDPs
    and " • "-separated (with a "PARABEN FREE INGREDIENTS:" label) on others.
    Kept verbatim; normalization happens downstream."""
    soup = BeautifulSoup(html, "lxml")
    for title in soup.select("a.accordion-title, a.accordion-toggle"):
        if title.get_text(strip=True).upper() != "INGREDIENTS":
            continue
        item = title.find_parent(class_="accordion-item")
        inner = item.select_one(".pdp-content-inner") if item is not None else None
        if inner is None:
            # US layout: the toggle's content section follows as a sibling
            inner = title.find_next(class_="pdp-content-inner")
        if inner is None:
            continue

        segments = []
        for element in inner.find_all(["p", "div"], recursive=False):
            text = " ".join(element.get_text(" ", strip=True).split())
            if text:
                segments.append(text)
        loose = " ".join(" ".join(inner.find_all(string=True, recursive=False)).split())
        if loose:
            segments.append(loose)

        best, best_dots = None, 0
        for segment in segments:
            dots = _inci_separator_count(segment)
            if dots > best_dots:
                best, best_dots = segment, dots
        if best is not None and best_dots >= _MIN_INCI_DOTS:
            best = _INCI_LABEL.sub("", best)
            return best.strip(" ·•") or None
        return None
    return None


class NarsAdapter:
    def __init__(
        self,
        fetcher: PoliteFetcher,
        brand_cfg: dict,
        ean_cache: EanCache,
        playwright: PlaywrightSession | None = None,
    ):
        self.fetcher = fetcher
        self.controller_base = str(brand_cfg["controller_base"]).rstrip("/") + "/"
        us_cfg = brand_cfg.get("us_fallback") or {}
        self.us_controller_base = (
            str(us_cfg["controller_base"]).rstrip("/") + "/" if us_cfg else None
        )
        self.ean_cache = ean_cache
        self.playwright = playwright
        self._escalated = False

    def _controller(self, name: str, base: str | None = None, **params: object) -> str:
        query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
        return f"{base or self.controller_base}{name}?{query}"

    def _get(self, url: str, *, referer: str | None, ajax: bool) -> CachedFetch:
        """httpx rung with automatic one-way escalation to Playwright."""
        if not self._escalated:
            try:
                return self.fetcher.get(
                    url, referer=referer, ajax=ajax, validator=_has_product_state
                )
            except BotShell:
                if self.playwright is None:
                    raise
                self._escalated = True
        if self.playwright is None:
            raise RuntimeError("escalated without a Playwright session")
        return self.playwright.get(url, referer=referer or url, ajax=ajax)

    def discover_master(self, gtin13: str) -> MasterResult:
        """EU site first; when the EU PDP is gone (410) or serves no product
        state, fall back to narscosmetics.com (same SFRA platform, site id
        nars_us). Same brand family, different region — callers ship
        US-sourced fields yellow."""
        show_url = self._controller("Product-Show", pid=gtin13)
        eu_error: str | None = None
        fetch = None
        try:
            fetch = self._get(show_url, referer=None, ajax=False)
        except FetchError as exc:
            eu_error = str(exc)
        state = parse_sfcc_product_state(fetch.text) if fetch is not None else None
        if state is None and eu_error is None:
            eu_error = f"{show_url}: no product-state object in PDP payload"

        region = "EU"
        if state is None:
            if self.us_controller_base is None:
                raise ValueError(eu_error)
            us_url = self._controller("Product-Show", base=self.us_controller_base, pid=gtin13)
            try:
                fetch = self._get(us_url, referer=None, ajax=False)
            except FetchError as exc:
                raise ValueError(f"EU: {eu_error}; US: {exc}") from exc
            state = parse_sfcc_product_state(fetch.text)
            if state is None:
                raise ValueError(
                    f"EU: {eu_error}; US: {us_url}: no product-state object in PDP payload"
                )
            region = "US"

        master_id = state.get("masterID") or state.get("ID")
        shades: dict[str, str] = {}
        for key, variant in (state.get("variants") or {}).items():
            if not key.startswith("color-"):
                continue
            shade = (variant.get("attributes") or {}).get("color")
            if shade:
                shades[variant["id"]] = shade

        # dwvar color val ids: join swatch-list shade names against the color
        # attribute's vals (val id == gtin13 on some masters, an internal
        # shade code on others). A variant can sit in the variants map yet be
        # MISSING from the purchasable vals (semi-delisted, e.g. the Orgasm
        # quad) — such gtins stay unmapped and resolve via their own PDP;
        # guessing a val would only fetch the master default.
        val_id_by_shade: dict[str, str] = {}
        val_ids: set[str] = set()
        for attribute in (state.get("variations") or {}).get("attributes") or []:
            if attribute.get("id") != "color":
                continue
            for val in attribute.get("vals") or []:
                shade_text = str(val.get("val") or "").casefold().strip()
                if val.get("id"):
                    val_ids.add(str(val["id"]))
                    if shade_text:
                        val_id_by_shade[shade_text] = str(val["id"])
        color_val_by_gtin: dict[str, str] = {}
        for gtin, shade in shades.items():
            if gtin in val_ids:
                color_val_by_gtin[gtin] = gtin
            elif shade.casefold().strip() in val_id_by_shade:
                color_val_by_gtin[gtin] = val_id_by_shade[shade.casefold().strip()]

        selected_id = str(state.get("ID") or gtin13)
        size_match = _SIZE_DIV.search(fetch.text)
        selected_shade = str(state.get("color") or state.get("productColor") or "").strip() or None
        if selected_shade is None:
            selected_shade = shades.get(selected_id)
        if selected_shade is None:
            selected_shade = jsonld_selected_shade(parse_jsonld_products(fetch.text))
        return MasterResult(
            master_id=str(master_id),
            product_name=str(state.get("name") or ""),
            pdp_url=fetch.final_url,
            discovered_via_gtin=gtin13,
            selected_id=selected_id,
            selected_shade=selected_shade,
            shade_by_gtin=shades,
            color_val_by_gtin=color_val_by_gtin,
            size_text=size_match.group(1).strip() if size_match else None,
            inci_text=extract_inci(fetch.text),
            inci_selected_gtin=selected_id,
            region=region,
            fallback_note=(f"EU site unavailable ({eu_error})" if region == "US" else None),
            fetched_at=fetch.fetched_at,
            from_cache=fetch.from_cache,
        )

    def variant_from_pdp(self, master: MasterResult) -> VariantResult:
        """The PDP itself as GTIN-anchored evidence — its product-state ID is
        the variant id. Used for simple products (no color swatch list) and
        for delisted shades whose own PDP still resolves."""
        gtin13 = master.selected_id
        ean12 = gtin13[1:] if len(gtin13) == 13 and gtin13.startswith("0") else gtin13
        result = VariantResult(
            gtin13=gtin13,
            ean12=ean12,
            ok=True,
            master_id=master.master_id,
            url=master.pdp_url,
            product_name=master.product_name,
            shade=master.selected_shade,
            size_text=master.size_text,
            returned_id=gtin13,
            snippet=f'"ID":"{gtin13}" (PDP product-state self-anchor)',
            fetched_at=master.fetched_at,
            from_cache=master.from_cache,
        )
        self.ean_cache.write(
            gtin13,
            {
                "gtin13": gtin13,
                "ean12": ean12,
                "master_id": master.master_id,
                "product_name": master.product_name,
                "shade": master.selected_shade,
                "size_text": master.size_text,
                "source_url": master.pdp_url,
                "pdp_url": master.pdp_url,
                "method": "dom",
                "via": "httpx",
                "fetched_at": (
                    master.fetched_at.isoformat(timespec="seconds") if master.fetched_at else None
                ),
            },
        )
        return result

    def resolve_variant(self, master: MasterResult, gtin13: str) -> VariantResult:
        color_val = master.color_val_by_gtin.get(gtin13, gtin13)
        base = self.us_controller_base if master.region == "US" else None
        url = self._controller(
            "Product-Variation",
            base=base,
            pid=master.master_id,
            **{f"dwvar_{master.master_id}_color": color_val},
            Quantity=1,
            format="ajax",
        )
        fetch = self._get(url, referer=master.pdp_url, ajax=True)
        state = parse_sfcc_product_state(fetch.text)

        ean12 = gtin13[1:] if len(gtin13) == 13 and gtin13.startswith("0") else gtin13
        result = VariantResult(
            gtin13=gtin13,
            ean12=ean12,
            ok=False,
            master_id=master.master_id,
            url=url,
            fetched_at=fetch.fetched_at,
            from_cache=fetch.from_cache,
            via=fetch.via,
        )

        if state is None:
            result.reject_reason = "no product-state object in variation partial"
            return result

        returned_id = str(state.get("ID") or "")
        result.returned_id = returned_id
        if returned_id != gtin13:
            # GTIN-anchor rule: never adopt a payload for a different variant
            result.reject_reason = (
                f"variation partial returned ID {returned_id!r}, requested {gtin13!r}"
            )
            return result

        # shade sources, most direct first: the partial's own selected-color
        # keys, the variants map (keyed by gtin13 on some masters, by color
        # val id on others), the master swatch list, JSON-LD
        shade = str(state.get("color") or state.get("productColor") or "").strip() or None
        if shade is None:
            variant_entry = (state.get("variants") or {}).get(f"color-{gtin13}") or (
                state.get("variants") or {}
            ).get(f"color-{color_val}", {})
            shade = (variant_entry.get("attributes") or {}).get("color")
        if shade is None:
            shade = master.shade_by_gtin.get(gtin13)
        if shade is None:
            shade = jsonld_selected_shade(parse_jsonld_products(fetch.text))
        size_match = _SIZE_DIV.search(fetch.text)

        result.ok = True
        result.product_name = str(state.get("name") or "")
        result.shade = shade
        result.size_text = size_match.group(1).strip() if size_match else None
        result.snippet = f'"ID":"{returned_id}" … "color":"{shade}"'

        self.ean_cache.write(
            gtin13,
            {
                "gtin13": gtin13,
                "ean12": ean12,
                "master_id": master.master_id,
                "product_name": result.product_name,
                "shade": shade,
                "size_text": result.size_text,
                "source_url": url,
                "pdp_url": master.pdp_url,
                "method": "sfcc_api",
                "via": result.via,
                "fetched_at": fetch.fetched_at.isoformat(timespec="seconds"),
            },
        )
        return result
