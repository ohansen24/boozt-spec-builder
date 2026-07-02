"""Field matrix cross-validation (build kit 6.5).

- Agreement across two independent source families -> VERIFIED (green).
- One family only -> SINGLE_SOURCE (yellow).
- Disagreement -> CONFLICT (red) with both values and URLs in notes.
- INCI: token-sequence compare; identical -> green-equivalent, differences
  confined to the May Contain block -> yellow with a rendered diff,
  base-list differences -> red. Weak sources (no GTIN) only ever add notes.
- ODM hints are tertiary: fuzzy name+shade similarity below threshold adds
  a note; a size disagreeing with the ODM hint downgrades to yellow.
"""

import re
from difflib import SequenceMatcher

from bsb.models import FieldValue, SourceRef

_MAY_CONTAIN = re.compile(
    r"\[?\s*\+\s*/?-?\s*\(?\s*may contain|may contain/peut contenir", re.IGNORECASE
)
_INCI_SPLIT = re.compile(r"\s*[·,;]\s*")
_PARENS = re.compile(r"\([^()]*\)")
_SIZE_TOKEN = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ml|g|pcs)\b", re.IGNORECASE)

NAME_CONFIRM_THRESHOLD = 0.6
ODM_NAME_NOTE_THRESHOLD = 0.5


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.casefold().strip(), b.casefold().strip()).ratio()


def combine_exact(
    field_label: str,
    primary_value: str | None,
    primary_ref: SourceRef | None,
    validator_value: str | None,
    validator_ref: SourceRef | None,
    notes: list[str] | None = None,
) -> FieldValue:
    """Exact (casefolded) agreement for strong signals like shade and size."""
    notes = list(notes or [])
    if primary_value is None:
        if validator_value is not None:
            # kit 6.5: one family = yellow — the retailer is GTIN-anchored, so
            # it may carry the field alone when the brand page lacks it
            # (seen live: delisted LRF Gobi has a degraded brand PDP)
            notes.append("brand site had no value; GTIN-anchored retailer is the single source")
            return FieldValue(
                value=validator_value,
                status="SINGLE_SOURCE",
                primary=validator_ref,
                notes="; ".join(notes),
            )
        return FieldValue(status="NOT_FOUND", notes="; ".join(notes) or f"{field_label} not found")

    if validator_value is None:
        notes.append("single source family (brand site); validator had no data")
        return FieldValue(
            value=primary_value, status="SINGLE_SOURCE", primary=primary_ref, notes="; ".join(notes)
        )

    if primary_value.casefold().strip() == validator_value.casefold().strip():
        notes.append("two independent families agree")
        return FieldValue(
            value=primary_value,
            status="VERIFIED",
            primary=primary_ref,
            secondary=validator_ref,
            notes="; ".join(notes),
        )

    notes.append(
        f"CONFLICT: brand says {primary_value!r} ({primary_ref.url if primary_ref else '?'}), "
        f"validator says {validator_value!r} ({validator_ref.url if validator_ref else '?'})"
    )
    return FieldValue(
        value=None,
        status="CONFLICT",
        primary=primary_ref,
        secondary=validator_ref,
        notes="; ".join(notes),
    )


def clean_retail_name(name: str, brand: str, known_shades: list[str] | None = None) -> str:
    """Strip retailer decoration (brand word, size, parentheticals, trailing
    shade) so name similarity compares substance, not styling."""
    cleaned = _PARENS.sub(" ", name)
    cleaned = _SIZE_TOKEN.sub(" ", cleaned)
    cleaned = re.sub(re.escape(brand), " ", cleaned, flags=re.IGNORECASE)
    for shade in known_shades or []:
        cleaned = re.sub(rf"\b{re.escape(shade)}\s*$", " ", cleaned, flags=re.IGNORECASE)
    return " ".join(cleaned.split())


def confirm_name(
    primary_value: str | None,
    primary_ref: SourceRef | None,
    retail_name: str | None,
    retail_ref: SourceRef | None,
    brand: str,
    known_shades: list[str] | None = None,
) -> FieldValue:
    """Retailer product names are decorated, never verbatim — they confirm a
    style_name only above a similarity threshold and never conflict it."""
    if primary_value is None:
        return FieldValue(status="NOT_FOUND", notes="style name not found")
    if retail_name is None:
        return FieldValue(
            value=primary_value,
            status="SINGLE_SOURCE",
            primary=primary_ref,
            notes="single source family (brand site); validator had no data",
        )

    cleaned = clean_retail_name(retail_name, brand, known_shades)
    # retailers abbreviate ("NARS Blush" for "Powder Blush") — token containment
    # of the cleaned retail name counts alongside plain similarity; the GTIN
    # anchor already ties the page to this exact product
    retail_tokens = set(cleaned.casefold().split())
    primary_tokens = set(primary_value.casefold().split())
    containment = len(retail_tokens & primary_tokens) / len(retail_tokens) if retail_tokens else 0.0
    score = max(similarity(primary_value, cleaned), containment)
    if score >= NAME_CONFIRM_THRESHOLD:
        return FieldValue(
            value=primary_value,
            status="VERIFIED",
            primary=primary_ref,
            secondary=retail_ref,
            notes=f"validator name {retail_name!r} matches (similarity {score:.2f})",
        )
    return FieldValue(
        value=primary_value,
        status="SINGLE_SOURCE",
        primary=primary_ref,
        notes=f"validator name {retail_name!r} too different to confirm "
        f"(similarity {score:.2f}) — retailer styling, not treated as a conflict",
    )


def split_inci(text: str) -> tuple[list[str], list[str]]:
    """(base tokens, may-contain tokens), casefolded."""
    match = _MAY_CONTAIN.search(text)
    base_part = text[: match.start()] if match else text
    may_part = text[match.start() :] if match else ""
    may_part = re.sub(_MAY_CONTAIN, " ", may_part)

    def tokens(segment: str) -> list[str]:
        out = []
        for token in _INCI_SPLIT.split(segment):
            cleaned = token.strip(" .[]():+/-·").casefold()
            if cleaned:
                out.append(cleaned)
        return out

    return tokens(base_part), tokens(may_part)


def compare_inci(a: str, b: str) -> tuple[str, str]:
    """("identical" | "may_contain_diff" | "base_diff", rendered diff)."""
    base_a, may_a = split_inci(a)
    base_b, may_b = split_inci(b)

    def render(missing: list[str], extra: list[str]) -> str:
        parts = []
        if missing:
            parts.append("missing: " + ", ".join(missing[:6]))
        if extra:
            parts.append("extra: " + ", ".join(extra[:6]))
        return " | ".join(parts)

    if base_a != base_b:
        missing = [t for t in base_a if t not in base_b]
        extra = [t for t in base_b if t not in base_a]
        if missing or extra:
            return "base_diff", render(missing, extra)
        return "base_diff", "same tokens, different order"
    if may_a != may_b:
        missing = [t for t in may_a if t not in may_b]
        extra = [t for t in may_b if t not in may_a]
        return "may_contain_diff", render(missing, extra)
    return "identical", ""


def odm_name_check(site_name: str, site_shade: str | None, odm_name: str) -> str | None:
    """Tertiary sanity: fuzzy similarity between the site's name+shade and the
    ODM's "Base - Shade" string; a low score adds a note, never a downgrade."""
    site_full = f"{site_name} - {site_shade}" if site_shade else site_name
    score = max(similarity(site_full, odm_name), similarity(site_name, odm_name))
    if score < ODM_NAME_NOTE_THRESHOLD:
        return (
            f"ODM calls this {odm_name!r} (similarity {score:.2f}) — "
            "brand site name differs; hint only, site is authoritative"
        )
    return None
