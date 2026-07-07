"""Phase 0 record assembly: ingest -> categorize -> normalize -> emit-ready.

Statuses follow the anti-hallucination charter with no network available:

- Fields that need web sources (style_name, color_name, size, ingredients)
  are NOT_FOUND. ODM hints go into notes as sanity context, never into the
  cell (principle 5: hints are not primary sources).
- country_iso and purchase_price come from the ODM, the two sanctioned
  ODM-primary fields here -> ODM_SOURCED with a SourceRef into the ODM file.
- category / color_code / flammable are rules-derived from the ODM name ->
  SINGLE_SOURCE (yellow: deterministic but unconfirmed by an independent
  source). Rules that cannot decide leave the field NOT_FOUND (fail closed).
- gender, length, variation are human-confirmed conventions from config /
  the template's own instructions -> MANUAL with a value (no review needed).
- style_number stays MANUAL and empty: the NARS prefix is unconfirmed
  (open question 1).
"""

import re
from datetime import UTC, datetime
from pathlib import Path

from bsb.categorize.rules import categorize, color_code_for
from bsb.ingest.odm import OdmParseResult, OdmRow
from bsb.models import FieldValue, ProductRecord, SourceRef
from bsb.normalize.boozt import normalize_size


def _odm_ref(odm_path: str, snippet: str, fetched_at: datetime) -> SourceRef:
    return SourceRef(
        url=Path(odm_path).as_posix(), method="odm", fetched_at=fetched_at, snippet=snippet
    )


def build_record(
    row: OdmRow,
    brand_key: str,
    brands: dict,
    rules: dict,
    odm_path: str,
    fetched_at: datetime,
) -> ProductRecord:
    brand_cfg = brands[brand_key]
    hints = row.hints

    record = ProductRecord(
        ean12=row.ean12,
        gtin13=row.gtin13,
        brand=str(brand_cfg.get("display_name", brand_key)),
        odm_hints=dict(hints),
    )

    size_hint = normalize_size(hints.get("size"), hints.get("size_unit"))
    record.style_name = FieldValue(
        status="NOT_FOUND", notes=f"requires web source (Phase 1); ODM name: {row.base_name}"
    )
    record.color_name = FieldValue(
        status="NOT_FOUND",
        notes=f"requires web source (Phase 1); ODM shade: {row.shade or '(none)'}",
    )
    record.size = FieldValue(
        status="NOT_FOUND",
        notes=f"requires web source (Phase 1); ODM hint: {size_hint or 'unparseable'}",
    )
    record.ingredients = FieldValue(status="NOT_FOUND", notes="requires web source (Phase 1)")

    gender_default = brand_cfg.get("gender_default")
    if gender_default:
        record.gender = FieldValue(
            value=str(gender_default),
            status="MANUAL",
            notes="brands.yaml default (per finished sheets); ODM blanket "
            f"'{hints.get('gender')}' ignored as unreliable",
        )

    decision = categorize(row.base_name, rules, brand_cfg)
    if decision.category:
        record.category = FieldValue(
            value=decision.category,
            status="SINGLE_SOURCE",
            notes=f"rule {decision.rule} on ODM name; web confirmation pending (Phase 1)",
        )
    else:
        record.category = FieldValue(
            status="NOT_FOUND", notes="no categorization rule matched — fail closed, never guessed"
        )

    cc = color_code_for(decision.category, row.shade, rules, brand_cfg, row.base_name)
    record.color_code = _color_code_field(cc, rules)

    if decision.category in rules["dg_trigger_categories"]:
        # DG rows are always red until a human confirms against the SDS (6.8)
        record.flammable = FieldValue(
            status="NOT_FOUND",
            notes=f"DG-trigger category {decision.category!r} — requires SDS "
            "review (Phase 1); never defaulted",
        )
    elif decision.category:
        record.flammable = FieldValue(
            value="No",
            status="SINGLE_SOURCE",
            notes=f"default for non-DG category {decision.category!r}",
        )
    else:
        record.flammable = FieldValue(status="NOT_FOUND", notes="category undecided")

    style_policy = brand_cfg.get("style_number_policy") or {}
    if style_policy.get("by_design_blank"):
        record.style_number = FieldValue(
            status="MANUAL", notes="by design: " + str(style_policy.get("note", "blank"))
        )
    else:
        record.style_number = FieldValue(
            status="MANUAL",
            notes="prefix unconfirmed — fill manually"
            if brand_cfg.get("style_prefix") is None
            else "left for manual entry",
        )

    coo = hints.get("coo")
    if coo not in (None, ""):
        record.country_iso = FieldValue(
            value=str(coo).strip(),
            status="ODM_SOURCED",
            primary=_odm_ref(odm_path, f"COO={coo} (row {row.row_number})", fetched_at),
            notes="ODM COO is the sanctioned source; note added if brand data disagrees (Phase 1)",
        )
    else:
        record.country_iso = FieldValue(status="NOT_FOUND", notes="ODM COO empty")

    record.extras["length"] = FieldValue(
        value="No Length", status="MANUAL", notes="template default: not applicable for beauty"
    )
    record.extras["variation"] = FieldValue(
        value="No Variant", status="MANUAL", notes="template default: not applicable for beauty"
    )
    price = hints.get("price")
    if price not in (None, ""):
        record.extras["purchase_price"] = FieldValue(
            value=str(price),
            status="ODM_SOURCED",
            primary=_odm_ref(odm_path, f"Client Price={price} (row {row.row_number})", fetched_at),
        )
    else:
        record.extras["purchase_price"] = FieldValue(status="NOT_FOUND", notes="no ODM price")
    expiry_default = brand_cfg.get("expiry_on_pack_default") or {}
    expiry_provided = hints.get("expiry")
    if expiry_provided not in (None, ""):
        # a Boozt "Specs" order sheet arrives with expiry pre-filled — honor
        # the provided value rather than dropping it to red
        record.extras["expiry_on_pack"] = FieldValue(
            value=str(expiry_provided).strip(),
            status="ODM_SOURCED",
            primary=_odm_ref(
                odm_path, f"expiry={expiry_provided} (row {row.row_number})", fetched_at
            ),
            notes="provided in the order sheet",
        )
    elif expiry_default.get("value"):
        record.extras["expiry_on_pack"] = FieldValue(
            value=str(expiry_default["value"]),
            status="VERIFIED",
            notes=str(expiry_default.get("note", "brand default")),
        )
    else:
        record.extras["expiry_on_pack"] = FieldValue(
            status="NOT_FOUND", notes="requires product knowledge"
        )

    return record


def _color_code_field(cc, rules: dict) -> FieldValue:
    """Foundation-family 1018 and the palette default are human-confirmed
    (green); other rule hits stay yellow pending confirmation; undecided
    fails closed."""
    cc_rules = rules.get("color_code_rules") or {}
    if cc.rule == "multi_shade_default" and cc_rules.get("multi_shade_note"):
        return FieldValue(
            value=str(cc.code),
            status="VERIFIED",
            notes=f"multi-shade product -> {cc.code}; {cc_rules['multi_shade_note']}",
        )
    if cc.code is None:
        if cc.rule == "multi_shade_product":
            return FieldValue(
                status="NOT_FOUND",
                notes="multi-shade product (quad/palette) — shade lexicon not applicable; "
                "needs Felina's product-type rule (dominant shade vs 1016 Multi-Colored)",
            )
        if cc.rule and cc.rule.startswith("signals_disagree"):
            # word and swatch-hex disagree -> withhold proposal, show both, red
            return FieldValue(
                status="NOT_FOUND",
                notes=f"color-code signals disagree ({cc.rule}) — needs human decision",
            )
        return FieldValue(status="NOT_FOUND", notes="no color-code rule matched — fail closed")
    confirmed_note = (rules.get("color_code_rules") or {}).get("foundation_family_note")
    if cc.rule == "foundation_family" and not cc.pending_confirmation and confirmed_note:
        return FieldValue(
            value=str(cc.code),
            status="VERIFIED",
            notes=f"rule {cc.rule}; {confirmed_note}",
        )
    if getattr(cc, "proposal", False):
        # auto-proposal (colour word / swatch hex): always yellow, clearly
        # marked so Felina's ingest-review can measure the correction rate and
        # so it never masquerades as a confirmed value or writes a lexicon.
        note = cc.proposal_note or "auto-proposed — please confirm or correct"
        return FieldValue(value=str(cc.code), status="SINGLE_SOURCE", notes=f"{note} [{cc.rule}]")
    notes = f"rule {cc.rule}"
    if cc.pending_confirmation:
        notes += " — pending confirmation"
    return FieldValue(value=str(cc.code), status="SINGLE_SOURCE", notes=notes)


def apply_order_overrides(
    records: list[ProductRecord], overrides: list[dict], source_path: str
) -> int:
    """Per-order human decisions (config/order_overrides/{order}.yaml)
    replace pipeline values; the override file becomes the deciding source in
    provenance (method "override"), the prior state is kept in the notes."""
    by_ean = {r.ean12: r for r in records}
    applied = 0
    for entry in overrides:
        field = str(entry["field"])
        value = str(entry["value"])
        status = str(entry.get("status", "VERIFIED"))
        decided_by = entry.get("decided_by", "?")
        date = str(entry.get("date", ""))
        rationale = str(entry.get("rationale", "")).strip()
        for ean in entry.get("eans", []):
            record = by_ean.get(str(ean))
            if record is None:
                continue
            prior: FieldValue = (
                getattr(record, field)
                if field in ProductRecord.field_values()
                else record.extras[field]
            )
            prior_note = f"prior: {prior.status}"
            if prior.notes:
                prior_note += f" — {prior.notes[:220]}"
            flag = (
                "VERIFY_AT_RECEIPT — confirm against physical goods at warehouse receipt; "
                if entry.get("verify_at_receipt")
                else ""
            )
            new_fv = FieldValue(
                value=value,
                status=status,
                primary=SourceRef(
                    url=source_path,
                    method="override",
                    fetched_at=datetime.now(UTC),
                    snippet=f"decided_by {decided_by} {date}: {rationale[:140]}",
                ),
                secondary=prior.primary,
                notes=f"{flag}override by {decided_by} ({date}): {rationale}; {prior_note}",
            )
            if field in ProductRecord.field_values():
                setattr(record, field, new_fv)
            else:
                record.extras[field] = new_fv
            applied += 1
    return applied


def build_records(
    odm_result: OdmParseResult,
    brand_key: str,
    brands: dict,
    rules: dict,
    odm_path: str,
) -> list[ProductRecord]:
    fetched_at = datetime.now(UTC)
    return [
        build_record(row, brand_key, brands, rules, odm_path, fetched_at) for row in odm_result.rows
    ]


# Cyrillic size units (ml/g) are matched deliberately; RUF001 ambiguity is ok
_TITLE_SIZE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(ml|ml\.|cl|l|g|gr|kg|oz|мл|мл\.|г|гр|мілілітрів|мілілітри)\b",  # noqa: RUF001
    re.IGNORECASE,
)
_UNIT_NORMALIZE = {
    "мл": "ml", "мл.": "ml", "мілілітрів": "ml", "мілілітри": "ml",
    "г": "g", "гр": "g", "gr": "g", "ml.": "ml",  # noqa: RUF001
}


def _size_from_title(name: str | None):
    """Parse a size embedded in a retailer title ("Cool Cream, 300 ml", "1000
    ml" in localized units), normalizing localized units to ml/g before the
    shared size normalizer. Returns the normalized size or None."""
    from bsb.normalize.boozt import normalize_size

    if not name:
        return None
    match = _TITLE_SIZE.search(name)
    if not match:
        return None
    number, unit = match.group(1), match.group(2).lower()
    unit = _UNIT_NORMALIZE.get(unit, unit)
    return normalize_size(f"{number} {unit}")


def build_retailer_inci_field(anchored, ref):
    """INCI FieldValue from GTIN-anchored retailer hits — the NO-BRAND path
    (there is no brand list to defer to). Under the EU-registered gate:
    EU/UK families preferred; two families agreeing on content -> green; a
    single family -> yellow; only non-EU sources -> yellow with the allergen
    caveat (never green). NEW (Oli R1/R5): two EQUAL-authority families that
    genuinely disagree on the base list, with no higher (brand) source to break
    the tie, fail closed to red rather than silently shipping the first and
    dropping the second. None when no hit carries INCI. Shared by the
    retailer-primary and field-completion passes so the rule lives in one place."""
    from bsb.resolve.market import inci_authority, is_eu_market
    from bsb.validate.matrix import compare_inci

    inci_hits = [h for h in anchored if h.inci]
    if not inci_hits:
        return None

    def _boozt(t):
        return t.replace(" · ", ", ").replace(" • ", ", ").strip(" ,")

    # highest INCI authority first (EU/UK retailer 3 > non-EU 2); no brand here
    inci_hits = sorted(inci_hits, key=lambda h: -inci_authority(h.market))
    first = inci_hits[0]
    others = [h for h in inci_hits if h.family != first.family]
    agree = next(
        (h for h in others if compare_inci(first.inci, h.inci)[0] == "identical"), None
    )
    if agree and is_eu_market(first.market):
        return FieldValue(
            value=_boozt(first.inci),
            status="VERIFIED",
            primary=ref(first),
            secondary=ref(agree),
            notes=f"two families agree, EU-sourced ({first.family}[{first.market}], "
            f"{agree.family}[{agree.market}]) — retailer",
        )
    # equal-authority disagreement, no brand list to defer to -> fail closed (red)
    conflict = next(
        (
            h
            for h in others
            if inci_authority(h.market) == inci_authority(first.market)
            and compare_inci(first.inci, h.inci)[0] == "base_diff"
        ),
        None,
    )
    if conflict is not None:
        _, diff = compare_inci(first.inci, conflict.inci)
        return FieldValue(
            value=None,
            status="CONFLICT",
            primary=ref(first),
            secondary=ref(conflict),
            notes=f"equal-authority retailer families disagree on base list [{diff}] "
            f"({first.family}[{first.market}] vs {conflict.family}[{conflict.market}]) — "
            "no brand list to defer to; fail closed — retailer",
        )
    note = (
        f"single EU/UK retailer family ({first.family}[{first.market}])"
        if is_eu_market(first.market)
        else f"non-EU market source ({first.family}[{first.market}]) — EU list may "
        "declare additional allergens"
    )
    return FieldValue(
        value=_boozt(first.inci),
        status="SINGLE_SOURCE",
        primary=ref(first),
        notes=note + "; retailer",
    )


def build_retailer_size_field(anchored, ref):
    """Size FieldValue from retailer hits: normalized-agreement across families
    (two agree -> green, one -> yellow), harvesting the size from the retail
    title when no explicit size field. None when no size is present."""
    from bsb.normalize.boozt import normalize_size

    sizes = [(h, normalize_size(h.size)) for h in anchored if normalize_size(h.size)]
    if not sizes:
        sizes = [(h, s) for h in anchored if (s := _size_from_title(h.name))]
    if not sizes:
        return None
    first = sizes[0]
    agree = [h for h, s in sizes if s == first[1]]
    return FieldValue(
        value=first[1],
        status="VERIFIED" if len(agree) >= 2 else "SINGLE_SOURCE",
        primary=ref(first[0]),
        secondary=ref(agree[1]) if len(agree) >= 2 else None,
        notes=("two retailer families agree" if len(agree) >= 2 else "single retailer family")
        + " (retailer)",
    )


def apply_retailer_primary(record, row, hits, brand_cfg, rules) -> None:
    """Fill a record with NO brand-site master from GTIN-anchored retailer
    hits (generic resolver). Retailer-primary policy: a field is GREEN only
    when two independent retailer families agree; a single family = yellow;
    everything else stays fail-closed. INCI single family = yellow (kit 6.5).
    """
    from datetime import UTC, datetime

    from bsb.categorize.rules import categorize, color_code_for
    from bsb.normalize.boozt import normalize_color_name, normalize_style_name
    from bsb.validate.language import is_english_name
    from bsb.validate.matrix import clean_retail_name, shades_agree, similarity

    brand = str(brand_cfg.get("display_name", ""))
    anchored = [h for h in hits if h.gtin_anchored]  # already ≤1 per family
    now = datetime.now(UTC)

    def ref(h):
        return SourceRef(url=h.url, method="dom", fetched_at=now, snippet=f"{h.family}: {h.name}")

    if not anchored:
        for f in ("style_name", "color_name", "size", "ingredients"):
            getattr(record, f).notes = "no GTIN-anchored retailer family found (retailer-primary)"
        return

    # --- style_name: English required (Boozt). Prefer English-source families;
    # a non-English name is NEVER shipped (never translated) — if that is all
    # that exists, fail closed with a note. Two English families agree -> green.
    named_all = [(h, clean_retail_name(h.name or "", brand)) for h in anchored if h.name]
    named = [(h, c) for h, c in named_all if is_english_name(h.name, h.language)]
    if named:
        base = named[0]
        agree = [
            h
            for h, c in named
            if similarity(base[1], c) >= 0.6 or base[1].casefold() in c.casefold()
        ]
        # ship the CLEANED name (brand + size stripped), not the raw retailer
        # title — else style_name carries "… - 250 ml"/brand prefix. Fall back to
        # the raw name only if cleaning emptied it (title was brand+size only).
        value = normalize_style_name(base[1] or base[0].name, brand_cfg)
        if len(agree) >= 2:
            record.style_name = FieldValue(
                value=value,
                status="VERIFIED",
                primary=ref(base[0]),
                secondary=ref(agree[1]),
                notes=f"two retailer families agree ({base[0].family}, {agree[1].family})",
            )
        else:
            record.style_name = FieldValue(
                value=value,
                status="SINGLE_SOURCE",
                primary=ref(base[0]),
                notes=f"single retailer family ({base[0].family}); retailer-primary",
            )
    elif named_all:
        # only non-English names exist -> fail closed, never ship/translate
        h0 = named_all[0][0]
        record.style_name = FieldValue(
            status="NOT_FOUND",
            primary=ref(h0),
            notes=f"only non-English sources found ({h0.language or '?'}, {h0.url})",
        )

    # --- size: normalized agreement / title harvest (shared builder)
    size_fv = build_retailer_size_field(anchored, ref)
    if size_fv is not None:
        record.size = size_fv

    # --- color_name: English required, same policy as style_name
    shaded_all = [(h, normalize_color_name(h.color, brand_cfg)) for h in anchored if h.color]
    shaded = [(h, c) for h, c in shaded_all if is_english_name(h.color, h.language)]
    if shaded:
        first = shaded[0]
        agree = [h for h, c in shaded if shades_agree(c or "", first[1] or "")]
        record.color_name = FieldValue(
            value=first[1],
            status="VERIFIED" if len(agree) >= 2 else "SINGLE_SOURCE",
            primary=ref(first[0]),
            secondary=ref(agree[1]) if len(agree) >= 2 else None,
            notes=("two retailer families agree" if len(agree) >= 2 else "single retailer family")
            + " (retailer-primary)",
        )
    elif shaded_all:
        h0 = shaded_all[0][0]
        record.color_name = FieldValue(
            status="NOT_FOUND",
            primary=ref(h0),
            notes=f"only non-English shade sources found ({h0.language or '?'}, {h0.url})",
        )

    # --- ingredients: EU-registered INCI gate (shared builder)
    inci_fv = build_retailer_inci_field(anchored, ref)
    if inci_fv is not None:
        record.ingredients = inci_fv

    # --- category/color_code/flammable from the resolved retailer name
    name_for_cat = record.style_name.value or (named[0][0].name if named else row.base_name)
    decision = categorize(name_for_cat or "", rules, brand_cfg)
    if decision.category:
        record.category = FieldValue(
            value=decision.category,
            status="SINGLE_SOURCE",
            primary=ref(anchored[0]),
            notes=f"rule {decision.rule} on retailer name; retailer-primary",
        )
        cc = color_code_for(
            decision.category, record.color_name.value or row.shade, rules, brand_cfg, name_for_cat
        )
        record.color_code = _color_code_field(cc, rules)
        if decision.category not in rules["dg_trigger_categories"]:
            record.flammable = FieldValue(
                value="No",
                status="SINGLE_SOURCE",
                notes=f"default for non-DG category {decision.category!r} (retailer-primary)",
            )


def _unpack_retailer_inci(retailer_inci):
    """(text, url, market) from a retailer_inci tuple, tolerant of the legacy
    2-tuple (text, url) — market defaults to None (treated as non-EU)."""
    if not retailer_inci:
        return None, None, None
    if len(retailer_inci) == 3:
        return retailer_inci
    text, url = retailer_inci
    return text, url, None


def apply_resolution(
    record: ProductRecord,
    row: OdmRow,
    resolved,  # ResolvedEan | None
    brand_cfg: dict,
    rules: dict,
    lf_product=None,  # LfProduct | None (validator family: name/shade/size)
    weak_inci=None,  # WeakInci | None (INCIDecoder, notes only)
    retailer_inci=None,  # (inci_text, source_url, market) | None — GTIN-anchored retailer
) -> list[str]:
    """Enrich a Phase 0 record with resolved brand-site data and the
    validator matrix (kit 6.5). Returns anomaly strings (site size vs ODM
    hint mismatches) for the run gate."""
    from bsb.categorize.rules import categorize, color_code_for
    from bsb.normalize.boozt import (
        convert_us_size,
        normalize_color_name,
        normalize_size,
        normalize_style_name,
    )
    from bsb.resolve.market import is_eu_market
    from bsb.validate.guide import check_name_length
    from bsb.validate.language import caps_review_tokens
    from bsb.validate.matrix import (
        combine_exact,
        compare_inci,
        confirm_name,
        is_alphabetized,
        odm_name_check,
        shades_agree,
        similarity,
    )

    anomalies: list[str] = []

    if resolved is None or not resolved.ok or resolved.variant is None:
        reason = (resolved.error if resolved else None) or "not resolved"
        for field in ("style_name", "color_name", "size", "ingredients"):
            fv: FieldValue = getattr(record, field)
            fv.notes = f"brand site: {reason}"
        return anomalies

    variant = resolved.variant
    master = resolved.master
    method = "sfcc_api" if variant.url != master.pdp_url else "dom"
    nars_ref = SourceRef(
        url=variant.url,
        method=method,
        fetched_at=variant.fetched_at or datetime.now(UTC),
        snippet=variant.snippet,
    )

    lf_variant = None
    lf_ref = None
    if lf_product is not None:
        lf_variant = lf_product.by_barcode.get(row.ean12)
        if lf_variant is not None:
            lf_ref = SourceRef(
                url=lf_product.url,
                method="dom",
                fetched_at=lf_product.fetched_at or datetime.now(UTC),
                snippet=f'"barcode":"{row.ean12}" … shade {lf_variant.shade!r}',
            )

    # --- style_name: brand-authoritative, retailer confirms above threshold
    site_name = normalize_style_name(variant.product_name, brand_cfg)
    known_shades = list(lf_product.by_barcode.values()) if lf_product else []
    record.style_name = confirm_name(
        site_name,
        nars_ref,
        lf_product.product_name if lf_variant else None,
        lf_ref,
        brand=str(brand_cfg.get("display_name", "")),
        known_shades=[v.shade for v in known_shades if v.shade],
    )
    if site_name and not check_name_length(site_name, rules):
        record.style_name.notes += "; EXCEEDS 60-char guide limit — shorten manually"
        record.style_name.status = "SINGLE_SOURCE"
    hint_note = odm_name_check(site_name or "", variant.shade, str(row.hints.get("name") or ""))
    if hint_note:
        record.style_name.notes += f"; {hint_note}"

    # --- color_name: exact match across families (normalized per brand
    # config; product-scoped overrides like Laguna's "Laguna 01" template)
    site_shade = normalize_color_name(variant.shade, brand_cfg, product_name=site_name)
    lf_shade = (
        normalize_color_name(lf_variant.shade, brand_cfg, product_name=site_name)
        if lf_variant
        else None
    )
    rejected_validator_shade = False
    no_color_code_settled = False
    if (
        site_shade is None
        and lf_shade is not None
        and row.shade
        # validator-only shade: gate against the ODM hint (kit 6.5 tertiary
        # check) — a retailer variant axis mislabeled as shade must not ship
        and not shades_agree(lf_shade, row.shade)
        and similarity(lf_shade, row.shade) < 0.5
    ):
        lf_shade = None
        rejected_validator_shade = True
        record.color_name = FieldValue(
            status="NOT_FOUND",
            primary=nars_ref,
            notes=f"brand site has no shade; validator value rejected — does not match "
            f"ODM hint {row.shade!r} (likely a non-shade variant axis)",
        )
    if site_shade is None and lf_shade is None:
        if getattr(variant, "shade_unresolved", False):
            # the product HAS a color axis but the shade is not on the current
            # site (discontinued shade dropped from the master swatch list) —
            # never label it colorless; fail closed for Felina / a retailer pass
            record.color_name = FieldValue(
                status="NOT_FOUND",
                primary=nars_ref,
                notes="shade-bearing variant but shade absent from current site "
                "(likely discontinued) — not a no-color row",
            )
        elif not rejected_validator_shade:
            aliases = {str(a).casefold() for a in rules.get("no_color_aliases", [])}
            hint = (row.shade or "").strip()
            if hint and hint.casefold() not in aliases:
                # the ODM names a real shade: this row is NOT shadeless — the
                # no-color convention must never be applied to it
                record.color_name = FieldValue(
                    status="NOT_FOUND",
                    primary=nars_ref,
                    notes=f"shade expected per ODM hint {hint!r} but no anchored source "
                    "asserts it — not a no-color row",
                )
            else:
                standard = rules.get("no_color_standard") or {}
                if standard.get("color_name"):
                    record.color_name = FieldValue(
                        value=str(standard["color_name"]),
                        status="VERIFIED",
                        primary=nars_ref,
                        notes="no shade on brand site — no-color standard; "
                        + str(standard.get("note", "")),
                    )
                    if standard.get("color_code"):
                        record.color_code = FieldValue(
                            value=str(standard["color_code"]),
                            status="VERIFIED",
                            notes="no-color standard; " + str(standard.get("note", "")),
                        )
                        no_color_code_settled = True
                else:
                    record.color_name = FieldValue(
                        status="NOT_FOUND",
                        primary=nars_ref,
                        notes="no shade on brand site — no-color convention pending "
                        "(open question 3)",
                    )
    else:
        record.color_name = combine_exact(
            "shade", site_shade, nars_ref, lf_shade, lf_ref, agree=shades_agree
        )
        # brands whose own site shades are authoritative and complete (Benefit's
        # numbered shades vs retailers' abbreviated titles): a retailer shade
        # CONFIRMS (agree -> green) but never CONFLICTS — keep the brand shade,
        # note the difference, so format mismatches don't fail the run.
        if (
            record.color_name.status == "CONFLICT"
            and brand_cfg.get("retailer_shade_confirms_only")
            and site_shade
        ):
            record.color_name = FieldValue(
                value=site_shade,
                status="SINGLE_SOURCE",
                primary=nars_ref,
                notes=f"brand-site shade authoritative; retailer differs ({lf_shade!r}) "
                "— confirm-only, not a conflict",
            )

    # caps-guard QA: if the SOURCE shade had a short all-caps token (title-cased,
    # ambiguous styling vs initialism), mark for a human eyeball (once per brand)
    caps = caps_review_tokens(variant.shade)
    if caps and record.color_name.value:
        record.color_name.notes = (
            (record.color_name.notes or "") + f" [caps-review: {','.join(caps)}]"
        )

    # --- size: exact match, then ODM tertiary check
    site_size, size_conversion_note = convert_us_size(variant.size_text)
    lf_size = normalize_size(lf_product.size_text) if lf_variant and lf_product.size_text else None
    record.size = combine_exact("size", site_size, nars_ref, lf_size, lf_ref)
    # brands whose PDP gives the exact per-variant volume (Benefit's SFCC size
    # description) are authoritative — a retailer that lists a different size is
    # usually a mislabelled full pack on a mini. Brand wins (yellow + note),
    # never a conflict-to-red, per the brand-first hierarchy.
    if (
        record.size.status == "CONFLICT"
        and site_size
        and brand_cfg.get("size_brand_authoritative")
    ):
        record.size = FieldValue(
            value=site_size,
            status="SINGLE_SOURCE",
            primary=nars_ref,
            notes=f"brand-site per-variant volume authoritative; retailer size differs "
            f"({lf_size!r}) — likely a mislabelled full pack, noted not conflicted",
        )
    if size_conversion_note:
        # Boozt needs metric; a converted US size always ships yellow
        if record.size.status == "VERIFIED":
            record.size.status = "SINGLE_SOURCE"
        record.size.notes += f"; {size_conversion_note}"
    odm_size = normalize_size(row.hints.get("size"), row.hints.get("size_unit"))
    if record.size.status == "CONFLICT" and odm_size and site_size and odm_size == site_size:
        # brand and ODM agree; exactly one validator disagrees -> yellow, not red
        record.size = FieldValue(
            value=site_size,
            status="SINGLE_SOURCE",
            primary=nars_ref,
            secondary=lf_ref,
            notes=f"brand and ODM agree on {site_size!r}; validator disagrees "
            f"({lf_size!r}, {lf_ref.url if lf_ref else '?'}) — outlier, noted not conflicted",
        )
    elif record.size.status == "CONFLICT" and odm_size:
        record.size.notes += f"; ODM hint: {odm_size}"
    if odm_size and record.size.value and odm_size != record.size.value:
        anomalies.append(
            f"{row.ean12} ({row.base_name}): site size {record.size.value!r} "
            f"!= ODM hint {odm_size!r}"
        )
        if record.size.status == "VERIFIED":
            record.size.status = "SINGLE_SOURCE"
        record.size.notes += f"; ODM hint disagrees ({odm_size}) — downgraded per kit 6.5"

    # a validator family (lookfantastic) that carries its own INCI is an
    # independent GTIN-anchored retailer source — fold it into retailer_inci
    # (with its market) when the generic pass supplied none, so it feeds the
    # same corroboration + EU-market gate below.
    if (retailer_inci is None or not retailer_inci[0]) and lf_product is not None:
        lf_inci = getattr(lf_product, "inci_text", None)
        if lf_inci:
            from bsb.resolve.market import classify_market

            retailer_inci = (lf_inci, lf_product.url, classify_market(lf_product.url))

    # --- ingredients: brand INCI (comma-space separators), weak support notes
    if master.inci_text:
        inci_boozt = (
            master.inci_text.replace(" · ", ", ")
            .replace(" • ", ", ")
            .replace("·", ",")
            .replace("•", ",")
            .strip(" ,")
        )
        notes = [
            f"brand INCI captured with shade {master.inci_selected_gtin} selected; "
            "one list per product (may-contain covers all shades)"
        ]
        if weak_inci and weak_inci.inci_text:
            verdict, diff = compare_inci(master.inci_text, weak_inci.inci_text)
            if verdict == "identical":
                notes.append(
                    f"INCIDecoder weak support: base list token-identical ({weak_inci.url})"
                )
            elif verdict == "may_contain_diff":
                notes.append(
                    "INCIDecoder weak support: may-contain block differs "
                    f"[{diff}] ({weak_inci.url})"
                )
            else:
                notes.append(
                    f"INCIDecoder weak support DISAGREES on base list [{diff}] ({weak_inci.url}) "
                    "— weak source, note only"
                )
        brand_inci_ref = SourceRef(
            url=master.pdp_url,
            method="dom",
            fetched_at=master.fetched_at or datetime.now(UTC),
            snippet=master.inci_text[:160],
        )
        # a GTIN-anchored retailer INCI is an independent family: agreement on
        # the base list -> VERIFIED green; may-contain-only diff -> yellow;
        # base-list diff -> CONFLICT (kit 6.5). The shipped value here is the
        # BRAND's (authoritative, EU-registered), so a corroborating retailer of
        # any market can confirm it to green — its market is only noted.
        ret_text, ret_url, ret_market = _unpack_retailer_inci(retailer_inci)
        if ret_text:
            verdict, diff = compare_inci(master.inci_text, ret_text)
            ret_ref = SourceRef(
                url=ret_url, method="dom", fetched_at=datetime.now(UTC), snippet=ret_text[:160]
            )
            # a lower-authority retailer may CONFIRM the brand list (-> green) or
            # ANNOTATE it (note), but NEVER delete or demote it: the shipped
            # value is always the brand's authoritative, EU-registered,
            # descending-weight list. A base-list difference is a visible yellow
            # annotation (real formulation differences must still surface — Oli
            # R5), not a red deletion (which inverted brand > retailer).
            alpha = (
                " — retailer base list is A-Z ordered (content corroboration "
                "only; shipped order is the brand's)"
                if is_alphabetized(ret_text)
                else ""
            )
            if verdict == "identical":
                record.ingredients = FieldValue(
                    value=inci_boozt,
                    status="VERIFIED",
                    primary=brand_inci_ref,
                    secondary=ret_ref,
                    notes="; ".join(
                        [
                            *notes,
                            f"retailer INCI content-identical ({ret_url})[{ret_market}]{alpha}",
                        ]
                    ),
                )
            elif verdict == "may_contain_diff":
                record.ingredients = FieldValue(
                    value=inci_boozt,
                    status="SINGLE_SOURCE",
                    primary=brand_inci_ref,
                    secondary=ret_ref,
                    notes="; ".join(
                        [*notes, f"retailer may-contain differs [{diff}] ({ret_url}){alpha}"]
                    ),
                )
            else:  # base_diff — genuine ingredient difference: annotate, keep brand
                record.ingredients = FieldValue(
                    value=inci_boozt,
                    status="SINGLE_SOURCE",
                    primary=brand_inci_ref,
                    secondary=ret_ref,
                    notes="; ".join(
                        [
                            *notes,
                            f"retailer base list differs [{diff}] ({ret_url})[{ret_market}]{alpha}"
                            " — brand authoritative (EU-registered, descending-weight order); "
                            "retailer annotated, not applied",
                        ]
                    ),
                )
        else:
            record.ingredients = FieldValue(
                value=inci_boozt,
                status="SINGLE_SOURCE",
                primary=brand_inci_ref,
                notes="; ".join(notes),
            )
    elif retailer_inci and retailer_inci[0]:
        # brand site carries no INCI — a single GTIN-anchored retailer is the
        # only source: ships yellow (kit 6.5 single family). If that source is
        # non-EU, add the EU-allergen caveat (Boozt requires EU-registered INCI)
        ret_text, ret_url, ret_market = _unpack_retailer_inci(retailer_inci)
        inci_boozt = (
            ret_text.replace(" · ", ", ")
            .replace(" • ", ", ")
            .replace("·", ",")
            .replace("•", ",")
            .strip(" ,")
        )
        if is_eu_market(ret_market):
            note = f"brand site has no INCI; single EU/UK retailer source ({ret_url})[{ret_market}]"
        else:
            note = (
                f"brand site has no INCI; non-EU market source ({ret_url})[{ret_market}] — "
                "EU list may declare additional allergens"
            )
        record.ingredients = FieldValue(
            value=inci_boozt,
            status="SINGLE_SOURCE",
            primary=SourceRef(
                url=ret_url, method="dom", fetched_at=datetime.now(UTC), snippet=ret_text[:160]
            ),
            notes=note,
        )
    else:
        record.ingredients = FieldValue(
            status="NOT_FOUND", primary=nars_ref, notes="no INGREDIENTS on brand PDP or retailer"
        )

    # --- category: the brand's own first-party site category (catalog-index
    # adapters) is the strongest signal; else the brand name, ODM only fallback
    site_category_id = getattr(master, "site_category_id", None)
    decision = categorize(site_name or row.base_name, rules, brand_cfg, site_category_id)
    if site_category_id and decision.rule and decision.rule.startswith("site_category:"):
        basis = "site categoryID"
    else:
        basis = "site name" if site_name else "ODM name"
    if decision.category is None and site_name:
        decision = categorize(row.base_name, rules, brand_cfg)
        basis = "ODM name (site name matched no rule)"
    if decision.category:
        record.category = FieldValue(
            value=decision.category,
            status="SINGLE_SOURCE",
            primary=nars_ref,
            notes=f"rule {decision.rule} on {basis}; enum-validated",
        )
    else:
        record.category = FieldValue(
            status="NOT_FOUND", notes="no categorization rule matched — fail closed, never guessed"
        )

    # --- color_code + flammable follow the final category
    if not no_color_code_settled:
        cc = color_code_for(
            decision.category,
            site_shade or row.shade,
            rules,
            brand_cfg,
            site_name or row.base_name,
            swatch_hex=getattr(master, "swatch_hex", None),
        )
        record.color_code = _color_code_field(cc, rules)

    if decision.category in rules["dg_trigger_categories"]:
        record.flammable = FieldValue(
            status="NOT_FOUND",
            notes=f"DG-trigger category {decision.category!r} — requires SDS review, "
            "never defaulted",
        )
    elif decision.category:
        record.flammable = FieldValue(
            value="No",
            status="SINGLE_SOURCE",
            notes=f"default for non-DG category {decision.category!r}",
        )
    else:
        record.flammable = FieldValue(status="NOT_FOUND", notes="category undecided")

    if master.region in ("US", "ARCHIVE"):
        # non-primary brand evidence never ships green on its own
        if master.region == "US":
            region_note = (
                f"US site fallback ({master.pdp_url}); {master.fallback_note or 'EU unavailable'}"
            )
        else:
            region_note = (
                "delisted from current site; filled from archived brand page "
                f"(snapshot {master.archived_at}, {master.pdp_url})"
            )
        for field in ("style_name", "color_name", "size", "ingredients"):
            fv: FieldValue = getattr(record, field)
            if fv.value is not None:
                if fv.status == "VERIFIED":
                    fv.status = "SINGLE_SOURCE"
                fv.notes = (fv.notes + "; " if fv.notes else "") + region_note

    return anomalies
