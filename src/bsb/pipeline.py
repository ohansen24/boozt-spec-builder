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

    cc = color_code_for(decision.category, row.shade, rules)
    if cc.code is not None:
        record.color_code = _color_code_field(cc, rules)
    else:
        record.color_code = FieldValue(
            status="NOT_FOUND", notes="no color-code rule matched — fail closed"
        )

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
    if expiry_default.get("value"):
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
    """Foundation-family 1018 is human-confirmed (green); other rule hits
    stay yellow pending web/lexicon confirmation."""
    confirmed_note = (rules.get("color_code_rules") or {}).get("foundation_family_note")
    if cc.rule == "foundation_family" and not cc.pending_confirmation and confirmed_note:
        return FieldValue(
            value=str(cc.code),
            status="VERIFIED",
            notes=f"rule {cc.rule}; {confirmed_note}",
        )
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


def apply_resolution(
    record: ProductRecord,
    row: OdmRow,
    resolved,  # ResolvedEan | None
    brand_cfg: dict,
    rules: dict,
    lf_product=None,  # LfProduct | None
    weak_inci=None,  # WeakInci | None
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
    from bsb.validate.guide import check_name_length
    from bsb.validate.matrix import (
        combine_exact,
        compare_inci,
        confirm_name,
        odm_name_check,
        shades_agree,
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
    if site_shade is None and lf_shade is None:
        record.color_name = FieldValue(
            status="NOT_FOUND",
            primary=nars_ref,
            notes="no shade on brand site — no-color convention pending (open question 3)",
        )
    else:
        record.color_name = combine_exact(
            "shade", site_shade, nars_ref, lf_shade, lf_ref, agree=shades_agree
        )

    # --- size: exact match, then ODM tertiary check
    site_size, size_conversion_note = convert_us_size(variant.size_text)
    lf_size = normalize_size(lf_product.size_text) if lf_variant and lf_product.size_text else None
    record.size = combine_exact("size", site_size, nars_ref, lf_size, lf_ref)
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
        record.ingredients = FieldValue(
            value=inci_boozt,
            status="SINGLE_SOURCE",
            primary=SourceRef(
                url=master.pdp_url,
                method="dom",
                fetched_at=master.fetched_at or datetime.now(UTC),
                snippet=master.inci_text[:160],
            ),
            notes="; ".join(notes),
        )
    else:
        record.ingredients = FieldValue(
            status="NOT_FOUND", primary=nars_ref, notes="no INGREDIENTS accordion on brand PDP"
        )

    # --- category: brand's own name is the taxonomy signal, ODM only fallback
    decision = categorize(site_name or row.base_name, rules, brand_cfg)
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
    cc = color_code_for(decision.category, site_shade or row.shade, rules)
    if cc.code is not None:
        record.color_code = _color_code_field(cc, rules)
    else:
        record.color_code = FieldValue(
            status="NOT_FOUND", notes="no color-code rule matched — fail closed"
        )

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
