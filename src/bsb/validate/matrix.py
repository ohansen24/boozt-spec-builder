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
import unicodedata
from difflib import SequenceMatcher

from bsb.models import FieldValue, SourceRef

_MAY_CONTAIN_WORD = re.compile(r"may\s+contain", re.IGNORECASE)
# trailing run of separator/prelude chars before the label ("· [+/-(" etc.)
_MAY_CONTAIN_PRELUDE = re.compile(r"[\[\(\+/\-\s·•,;:]*$")
_INCI_SPLIT = re.compile(r"\s*[·•,;]\s*")
_PARENS = re.compile(r"\([^()]*\)")
_SIZE_TOKEN = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ml|g|pcs)\b", re.IGNORECASE)

NAME_CONFIRM_THRESHOLD = 0.6
ODM_NAME_NOTE_THRESHOLD = 0.5


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.casefold().strip(), b.casefold().strip()).ratio()


def _fold_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def shades_agree(a: str, b: str) -> bool:
    """Shade IDENTITY comparison across sources that style shades differently
    (all live cases): 'Café Con Leche' vs 'Cafe Con Leche' (accents),
    'Laguna 01' vs '1' (retailer publishes the bare shade number),
    '888 Dolce Vita' vs 'Dolce Vita' (brand prefixes the shade number).
    Word tokens and numeric tokens are compared separately; a side missing
    one kind defers to the other side on it."""
    fa = _fold_accents(a).casefold().strip()
    fb = _fold_accents(b).casefold().strip()
    if fa == fb:
        return True

    def parts(text: str) -> tuple[list[int], list[str]]:
        digits = [int(d) for d in re.findall(r"\d+", text)]
        words = re.findall(r"[a-z]+", text)
        return digits, words

    digits_a, words_a = parts(fa)
    digits_b, words_b = parts(fb)
    digits_match = digits_a == digits_b or not digits_a or not digits_b
    words_match = words_a == words_b or not words_a or not words_b
    # at least one dimension must positively match, the other may be absent
    has_positive = (digits_a and digits_a == digits_b) or (words_a and words_a == words_b)
    return bool(digits_match and words_match and has_positive)


def combine_exact(
    field_label: str,
    primary_value: str | None,
    primary_ref: SourceRef | None,
    validator_value: str | None,
    validator_ref: SourceRef | None,
    notes: list[str] | None = None,
    agree=None,
) -> FieldValue:
    """Exact (casefolded) agreement for strong signals like shade and size.
    `agree(a, b) -> bool` overrides the comparison (shade identity)."""
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

    matches = (
        agree(primary_value, validator_value)
        if agree is not None
        else primary_value.casefold().strip() == validator_value.casefold().strip()
    )
    if matches:
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


_WATER_WORDS = {"water", "aqua", "eau", "purified water"}


def _water_canon(token: str) -> str:
    """Compare-time ONLY (kit: never rewrite the stored value): multi-language
    water naming is one ingredient — Water == Aqua == Eau == Water/Aqua/Eau ==
    "Aqua (Purified Water)"."""
    # token edge-cleaning may have eaten a closing paren — match it optional
    base = re.sub(r"\([^)]*\)?", "", token).strip()
    parts = [p.strip() for p in base.split("/") if p.strip()]
    if parts and all(p in _WATER_WORDS for p in parts):
        return "aqua"
    return token


def split_inci(text: str) -> tuple[list[str], list[str]]:
    """(base tokens, may-contain tokens), casefolded. The may-contain label
    varies wildly across sources ("[+/-(MAY CONTAIN/PEUT CONTENIR):",
    "May Contain/Peut Contenir/(+/-):") — everything from the label's prelude
    through its colon is consumed as marker, never as tokens."""
    match = _MAY_CONTAIN_WORD.search(text)
    if match is None:
        base_part, may_part = text, ""
    else:
        prelude = _MAY_CONTAIN_PRELUDE.search(text[: match.start()])
        base_part = text[: prelude.start()] if prelude else text[: match.start()]
        rest = text[match.end() :]
        colon = rest.find(":")
        may_part = rest[colon + 1 :] if 0 <= colon <= 40 else rest

    def tokens(segment: str) -> list[str]:
        out = []
        for token in _INCI_SPLIT.split(segment):
            # collapse ALL whitespace (hand-filled cells embed newlines that
            # otherwise shield trailing brackets from the strip); spacing
            # around "/" is styling ("butylene/ ethylene" == "butylene/ethylene")
            cleaned = " ".join(token.split()).replace(" / ", "/").replace("/ ", "/")
            cleaned = cleaned.replace(" /", "/").strip(" .[]():+/-·").casefold()
            if cleaned:
                out.append(_water_canon(cleaned))
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
