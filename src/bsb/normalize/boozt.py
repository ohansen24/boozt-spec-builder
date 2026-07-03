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


_NUMERIC_SUFFIX = re.compile(r"\s+[–—-]\s+\d+\s*$")  # noqa: RUF001 - site uses en dash


def _brand_title_case(text: str) -> str:
    """Title-case a site ALL-CAPS string, keeping digits/punctuation intact.
    Only applied when the brand config asks for it."""
    return re.sub(r"[A-Za-zÀ-ÿ']+", lambda m: m.group(0).capitalize(), text)


_OZ = re.compile(r"(\d+(?:\.\d+)?)\s*(fl\.?\s*oz|oz)\b", re.IGNORECASE)


def convert_us_size(value: object) -> tuple[str | None, str | None]:
    """US imperial size -> Boozt metric ("0.18 fl oz" -> "5.3 ml",
    "0.14 oz" -> "4 g"). Returns (metric size, conversion note) or
    (None, None). Boozt needs metric; converted values ship yellow."""
    if value is None:
        return None, None
    text = clean_ws(str(value))
    metric = normalize_size(text)
    if metric is not None:
        return metric, None  # already metric, no conversion needed
    m = _OZ.search(text)
    if not m:
        return None, None
    amount = float(m.group(1))
    if "fl" in m.group(2).lower():
        converted, unit = amount * 29.5735, "ml"
    else:
        converted, unit = amount * 28.3495, "g"
    rounded = round(converted, 1)
    number = str(int(rounded)) if rounded == int(rounded) else str(rounded)
    return f"{number} {unit}", f"converted from US size {text!r} ({m.group(0)})"


def normalize_color_name(
    value: object, brand_cfg: dict | None = None, product_name: str | None = None
) -> str | None:
    """The brand's exact shade string, verbatim, trimmed — optionally passed
    through the brand's shade_format config (e.g. NARS site styling
    "ORGASM - 777" (site uses an en dash) -> "Orgasm"; provisional). The
    verbatim site string always stays in provenance; formatting alone never
    changes a cell's status."""
    if value is None:
        return None
    cleaned = clean_ws(str(value))
    if not cleaned:
        return None
    if product_name:
        overrides = (brand_cfg or {}).get("shade_format_overrides") or {}
        pn = product_name.casefold()
        for key, override in overrides.items():
            if str(key).casefold() not in pn:
                continue
            template = override.get("number_template")
            digits = re.search(r"\d+", cleaned)
            if template and digits:
                return template.format(number=int(digits.group()))
            break  # matched product but no applicable rule -> default formatting
    fmt = (brand_cfg or {}).get("shade_format") or {}
    if fmt.get("strip_numeric_suffix"):
        cleaned = _NUMERIC_SUFFIX.sub("", cleaned).strip()
    if fmt.get("title_case"):
        cleaned = _brand_title_case(cleaned)
    return cleaned or None


def normalize_style_name(value: object, brand_cfg: dict | None = None) -> str | None:
    """Master product name; optionally title-cased per brand name_format
    (NARS publishes ALL CAPS)."""
    if value is None:
        return None
    cleaned = clean_ws(str(value))
    if not cleaned:
        return None
    if ((brand_cfg or {}).get("name_format") or {}).get("title_case"):
        cleaned = _brand_title_case(cleaned)
    return cleaned or None
