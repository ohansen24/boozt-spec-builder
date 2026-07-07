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
    shade) so a retailer product title becomes the bare product name — used both
    to compare name substance and, on the retailer-primary path, as the shipped
    style_name. Edge separators left behind after removing a brand prefix or
    trailing size ("Maria Nila - Shaping Heat Spray - 250 ml" -> "Shaping Heat
    Spray") are trimmed; an internal dash in the real name is kept."""
    cleaned = _PARENS.sub(" ", name)
    cleaned = _SIZE_TOKEN.sub(" ", cleaned)
    cleaned = re.sub(re.escape(brand), " ", cleaned, flags=re.IGNORECASE)
    for shade in known_shades or []:
        cleaned = re.sub(rf"\b{re.escape(shade)}\s*$", " ", cleaned, flags=re.IGNORECASE)
    return " ".join(cleaned.split()).strip(" -–—·,|/")  # noqa: RUF001 (en/em dash separators)


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


# --- compare-time INCI folding (kit: NEVER rewrite the stored value) ---------
# Boozt requires the brand's descending-weight ordering and EU nomenclature;
# retailers reorder, alphabetize, localize and abbreviate. These tables let the
# comparator recognise the SAME ingredient across those benign variants so a
# formatting difference is not mistaken for a formulation conflict. Versioned:
# bump when the table changes. Observed live (Maria Nila 2026-07): German
# localisations, spelling variants, generic/specific fragrance naming.
# Per-family capability (which retailers alphabetize / carry unusable INCI) is
# documented in config/validators.yaml (inci_capability).
INCI_SYNONYM_VERSION = 1

# slash-joined multi-language names that denote ONE ingredient
_SLASH_SYNONYM_GROUPS = {
    "aqua": {"water", "aqua", "eau", "purified water"},
    "parfum": {"parfum", "fragrance"},
}
# whole-token synonyms (retailer spelling/localisation -> EU canonical)
_TOKEN_SYNONYMS = {
    "propandiol": "propanediol",
    "butan": "butane",
    "isobutan": "isobutane",
    "cetrimoniumchlorid": "cetrimonium chloride",
    "tocopherylacetat": "tocopheryl acetate",
    "polyvinylpyrrolidon": "pvp",
    "propylenglykol": "propylene glycol",
}
# word-boundary folds applied within multi-word tokens
_WORD_FOLDS = {
    "annus": "annuus",          # "helianthus annus" -> annuus (retailer typo)
    "hydrolized": "hydrolyzed",  # US/retailer spelling
}
_MATCH_STRIP = re.compile(r"[\s\-]")


def _slash_canon(token: str) -> str:
    """Compare-time ONLY: a slash-joined name whose parts are all one synonym
    group collapses to that group's canonical token — Water == Aqua == Eau ==
    Water/Aqua/Eau == "Aqua (Purified Water)"; Parfum == Fragrance ==
    Parfum/Fragrance."""
    # token edge-cleaning may have eaten a closing paren — match it optional
    base = re.sub(r"\([^)]*\)?", "", token).strip()
    parts = [p.strip() for p in base.split("/") if p.strip()]
    if not parts:
        return token
    for canon, members in _SLASH_SYNONYM_GROUPS.items():
        if all(p in members for p in parts):
            return canon
    return token


def _match_key(token: str) -> str:
    """Order/space/hyphen/localisation-insensitive equality key for a token
    (compare-time ONLY). Folds known synonyms then strips spacing and hyphens
    so "Cetearyl Alcohol"=="Cetearylalcohol", "Alpha-Isomethyl Ionone"==
    "Alphaisomethyl Ionone", "Quaternium-95"=="Quaternium95". Distinct
    ingredients stay distinct (Glycerin != Glycerine — not in the table)."""
    t = _TOKEN_SYNONYMS.get(token, token)
    for word, repl in _WORD_FOLDS.items():
        t = re.sub(rf"\b{word}\b", repl, t)
    # drop DIGIT-FREE clarifying parentheticals ("(Shea)", "(Potato)",
    # "(Sunflower)") so an omitted common name folds — but KEEP parentheticals
    # carrying a numeric identity ("(CI 77491)"), else two distinct colorants
    # written "Iron Oxides (CI 77491)" / "(CI 77499)" would collapse to one key.
    t = re.sub(r"\((?![^)]*\d)[^)]*\)", "", t)
    return _MATCH_STRIP.sub("", t)


def is_alphabetized(text: str) -> bool:
    """True when a candidate's base list is sorted A-Z: it carries no usable
    concentration ordering, so it can corroborate CONTENT but never supply
    order (which only the brand publishes). Floor of 8 tokens: a short real
    formula can open Aqua < … < Parfum by coincidence, but a full ingredient
    list being A-Z end-to-end by chance (rather than by an alphabetizing
    retailer) is astronomically unlikely."""
    base, _ = split_inci(text)
    return len(base) >= 8 and base == sorted(base)


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
                out.append(_slash_canon(cleaned))
        return out

    return tokens(base_part), tokens(may_part)


def compare_inci(a: str, b: str) -> tuple[str, str]:
    """("identical" | "may_contain_diff" | "base_diff", rendered diff).

    CONTENT comparison, order-neutral (kit 6.5 + Oli 2026-07): ingredient
    identity is compared as a set of fold-normalised match keys, so a retailer
    that reorders, alphabetizes or localises the SAME formula reads as
    identical — a difference in ORDER is never a conflict, because Boozt ships
    the brand's descending-weight ordering regardless. A real ingredient
    difference (a token present on one side only, after folding) still surfaces
    as base_diff / may_contain_diff with a rendered diff, so genuine
    formulation differences are never silently swallowed."""
    base_a, may_a = split_inci(a)
    base_b, may_b = split_inci(b)

    def keys(tokens: list[str]) -> set[str]:
        return {_match_key(t) for t in tokens}

    def render(src_a: list[str], keys_b: set[str], src_b: list[str], keys_a: set[str]) -> str:
        missing = [t for t in src_a if _match_key(t) not in keys_b]
        extra = [t for t in src_b if _match_key(t) not in keys_a]
        parts = []
        if missing:
            parts.append("missing: " + ", ".join(missing[:6]))
        if extra:
            parts.append("extra: " + ", ".join(extra[:6]))
        return " | ".join(parts)

    ka, kb = keys(base_a), keys(base_b)
    if ka != kb:
        return "base_diff", render(base_a, kb, base_b, ka)
    kma, kmb = keys(may_a), keys(may_b)
    if kma != kmb:
        return "may_contain_diff", render(may_a, kmb, may_b, kma)
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
