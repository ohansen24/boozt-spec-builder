"""Boozt Guide v1.3 normalization rules (build kit 6.6), driven by
config/boozt_rules.yaml. Values that cannot be normalized return None —
enums fail closed, nothing is guessed.
"""

import re

_SIZE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(ml|g|gr|grams?|pcs|pc|pieces?)\.?\s*$", re.IGNORECASE)
_UNIT_ALIASES = {
    "ml": "ml",
    "g": "g",
    "gr": "g",
    "gram": "g",
    "grams": "g",
    "pcs": "pcs",
    "pc": "pcs",
    "piece": "pcs",
    "pieces": "pcs",
}
_ONE_SIZE = re.compile(r"^\s*one\s*size\s*$", re.IGNORECASE)


def clean_ws(value: str) -> str:
    """Global whitespace lint (regression cases 2 and 4): non-breaking spaces
    become regular spaces, leading/trailing whitespace is stripped. Interior
    spacing is preserved — values are verbatim otherwise."""
    return value.replace("\xa0", " ").strip()


def normalize_size(value: object, unit: object = None) -> str | None:
    """Normalize to "{value} {unit}" with dot decimals and unit in {ml, g, pcs},
    or "One Size". "4,4gr" -> "4.4 g"; ("4.4", "GR") -> "4.4 g". None if the
    input cannot be parsed."""
    if value is None:
        return None
    text = clean_ws(str(value))
    if _ONE_SIZE.match(text):
        return "One Size"

    unit_text = clean_ws(str(unit)) if unit not in (None, "") else ""
    candidate = f"{text} {unit_text}" if unit_text else text
    m = _SIZE.match(candidate)
    if not m:
        return None
    number, raw_unit = m.groups()
    normalized_unit = _UNIT_ALIASES.get(raw_unit.lower())
    if normalized_unit is None:
        return None
    number = number.replace(",", ".")
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    return f"{number} {normalized_unit}"


def normalize_category(value: object, rules: dict) -> str | None:
    """Case-insensitive match against the guide enum, returning the canonical
    display form. Fails closed on anything else (principle 4)."""
    if value is None:
        return None
    cleaned = clean_ws(str(value)).casefold()
    for category in rules["categories"]:
        if category.casefold() == cleaned:
            return category
    return None


def normalize_color_name(value: object) -> str | None:
    """The brand's exact shade string, verbatim, trimmed. No translation."""
    if value is None:
        return None
    cleaned = clean_ws(str(value))
    return cleaned or None


def normalize_style_name(value: object) -> str | None:
    if value is None:
        return None
    cleaned = clean_ws(str(value))
    return cleaned or None
