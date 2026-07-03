"""Golden comparison (Phase 2, Stage 2): run the resolve layer offline from
probe fixtures/cache and diff auto-fillable fields against Felina's finished
sheets (the answer keys), using the compare-external classification. The
per-brand agreement rate is our reliability evidence before a first order.

Cache-first: after a probe, every needed payload is in cache/http, so this
runs without network. Fields the platform does not expose are reported as
not-comparable, never as disagreements.
"""

import re

from pydantic import BaseModel, Field

from bsb.compare import _classify_values
from bsb.fetch.cache import EanCache, HttpCache
from bsb.fetch.ladder import FetchError, PoliteFetcher
from bsb.ingest.template import read_sheet_rows
from bsb.normalize.boozt import normalize_size

_TAGS = re.compile(r"<[^>]+>")
_INCI_HINT = re.compile(
    r"\b(aqua|water|glycerin|parfum|dimethicone|phenoxyethanol)\b", re.IGNORECASE
)
_SIZE_IN_TEXT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(ml|g|oz|fl\.?\s*oz)\b", re.IGNORECASE)


class FieldStats(BaseModel):
    agree: int = 0
    format_only: int = 0
    disagree: int = 0
    tool_missing: int = 0
    theirs_missing: int = 0

    @property
    def comparable(self) -> int:
        return self.agree + self.format_only + self.disagree

    @property
    def rate(self) -> float:
        return (self.agree + self.format_only) / self.comparable if self.comparable else 0.0


class GoldenResult(BaseModel):
    brand: str
    rows: int = 0
    resolved: int = 0
    fields: dict[str, FieldStats] = Field(default_factory=dict)
    disagreements: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _inci_from_html(html: str | None) -> str | None:
    """Longest comma-dense INCI-looking run in a product description."""
    if not html:
        return None
    text = _TAGS.sub(" ", html)
    text = re.sub(r"(?i)full ingredients?( list)?\s*:?\s*", "", text)
    best = None
    for chunk in re.split(r"\n|(?<=[.!?])\s{2,}", text):
        chunk = " ".join(chunk.split())
        if (
            chunk.count(",") >= 8
            and len(_INCI_HINT.findall(chunk)) >= 2
            and (best is None or len(chunk) > len(best))
        ):
            best = chunk
    return best


def _size_from_text(*texts: str | None) -> str | None:
    for text in texts:
        if not text:
            continue
        match = _SIZE_IN_TEXT.search(text)
        if match:
            return normalize_size(f"{match.group(1)}{match.group(2)}")
    return None


def _resolve_shopify(
    brand_cfg: dict, gtin13: str, fetcher: PoliteFetcher, ean_cache: EanCache, adapter_cache: dict
):
    from bsb.resolve.adapters.shopify import ShopifyAdapter

    adapter = adapter_cache.get("shopify")
    if adapter is None:
        adapter = ShopifyAdapter(fetcher, brand_cfg, ean_cache)
        adapter_cache["shopify"] = adapter
    hit = adapter.resolve_variant(gtin13)
    if not hit.ok:
        return None
    title = hit.product_title or ""
    # strip a trailing size from the title for the style-name comparison
    clean_title = _SIZE_IN_TEXT.sub("", title).strip(" -–,")  # noqa: RUF001
    from bsb.resolve.adapters.shopify import _SHADE_OPTIONS, _SIZE_OPTIONS

    shade = next(
        (v for k, v in hit.variant_options.items() if k.casefold() in _SHADE_OPTIONS), None
    )
    size_opt = next(
        (v for k, v in hit.variant_options.items() if k.casefold() in _SIZE_OPTIONS), None
    )
    return {
        "style_name": clean_title or None,
        "color_name": shade,
        "size": _size_from_text(size_opt, hit.variant_title, title),
        "ingredients": _inci_from_html(hit.body_html),
    }


def _resolve_sfcc(
    brand_cfg: dict, gtin13: str, fetcher: PoliteFetcher, ean_cache: EanCache, adapter_cache: dict
):
    from bsb.resolve.adapters.sfcc import SfccAdapter

    adapter = adapter_cache.get("sfcc")
    if adapter is None:
        adapter = SfccAdapter(fetcher, brand_cfg, ean_cache)
        adapter_cache["sfcc"] = adapter
    try:
        master = adapter.discover_master(gtin13)
    except (FetchError, ValueError):
        return None
    if master.selected_id != gtin13:
        return None
    return {
        "style_name": master.product_name or None,
        "color_name": master.selected_shade,
        "size": normalize_size(master.size_text) if master.size_text else None,
        "ingredients": master.inci_text,
    }


GOLDEN_FIELDS = ("style_name", "color_name", "size", "ingredients")


def golden_compare(
    brand_key: str,
    brand_cfg: dict,
    answer_sheet: str,
    synonyms: dict,
    cache_dir,
    limit: int | None = None,
) -> GoldenResult:
    result = GoldenResult(brand=brand_key)
    adapter_type = brand_cfg.get("adapter")
    if adapter_type not in ("shopify", "sfcc", "nars_sfcc"):
        result.notes.append(f"no resolve-capable adapter ({adapter_type!r}) — golden not runnable")
        return result
    resolver = _resolve_shopify if adapter_type == "shopify" else _resolve_sfcc

    fetcher = PoliteFetcher(HttpCache(cache_dir))
    ean_cache = EanCache(cache_dir)
    adapter_cache: dict = {}
    rows = read_sheet_rows(answer_sheet, synonyms)
    if limit:
        rows = rows[:limit]
    result.fields = {f: FieldStats() for f in GOLDEN_FIELDS}

    try:
        for row in rows:
            result.rows += 1
            ean = re.sub(r"\D", "", str(row.get("ean") or ""))
            gtin13 = ean if len(ean) == 13 else "0" + ean
            resolved = resolver(brand_cfg, gtin13, fetcher, ean_cache, adapter_cache)
            if resolved is None:
                for field in GOLDEN_FIELDS:
                    if row.get(field) not in (None, ""):
                        result.fields[field].tool_missing += 1
                continue
            result.resolved += 1
            for field in GOLDEN_FIELDS:
                ours, theirs = resolved.get(field), row.get(field)
                ours_empty = ours in (None, "")
                theirs_empty = theirs in (None, "") or (
                    isinstance(theirs, str) and not theirs.strip()
                )
                if ours_empty and theirs_empty:
                    continue
                if ours_empty:
                    result.fields[field].tool_missing += 1
                    continue
                if theirs_empty:
                    result.fields[field].theirs_missing += 1
                    continue
                classification, note = _classify_values(field, ours, theirs)
                if classification == "AGREE":
                    result.fields[field].agree += 1
                elif classification == "FORMAT_ONLY":
                    result.fields[field].format_only += 1
                else:
                    result.fields[field].disagree += 1
                    result.disagreements.append(
                        f"{ean} {field}: tool {str(ours)[:60]!r} vs felina {str(theirs)[:60]!r}"
                        + (f" [{note}]" if note else "")
                    )
    finally:
        fetcher.close()
    return result
