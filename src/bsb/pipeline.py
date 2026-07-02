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
        notes = f"rule {cc.rule}"
        if cc.pending_confirmation:
            notes += " — pending confirmation (open question 2)"
        record.color_code = FieldValue(value=str(cc.code), status="SINGLE_SOURCE", notes=notes)
    else:
        record.color_code = FieldValue(
            status="NOT_FOUND", notes="no color-code rule matched — fail closed"
        )

    if decision.category:
        record.flammable = FieldValue(
            value="No",
            status="SINGLE_SOURCE",
            notes=f"default for non-DG category {decision.category!r} "
            "(this order contains no DG categories)",
        )
    else:
        record.flammable = FieldValue(status="NOT_FOUND", notes="category undecided")

    record.style_number = FieldValue(
        status="MANUAL",
        notes="NARS style-number prefix unconfirmed (open question 1) — fill manually"
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
    record.extras["expiry_on_pack"] = FieldValue(
        status="NOT_FOUND", notes="requires product knowledge (Phase 1)"
    )

    return record


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
