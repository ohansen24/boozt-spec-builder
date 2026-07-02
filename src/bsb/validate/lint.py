"""Lint rules for filled Boozt sheets — the five observed manual errors from
build kit section 1 are the regression cases:

1. STYLE_NUMBER_FOREIGN   an embedded EAN that is not the row's EAN, or a
                          style prefix belonging to a different brand
                          ("SVR3662361001699" in a NARS order)
2. WHITESPACE             non-breaking spaces / leading-trailing whitespace
                          (" Makeup " with NBSPs)
3. SIZE_FORMAT            size not in guide format ("4,4gr" instead of "4.4 g")
4. WHITESPACE             trailing space in a shade name ("Orgasm ")
5. NO_COLOR_CONVENTION    inconsistent no-color conventions (CLEAR vs
                          NO COLOR vs NO SHADE) — open question 3
"""

import re

from pydantic import BaseModel

from bsb.normalize.boozt import clean_ws, normalize_size
from bsb.validate.guide import check_category, check_size_format

_DIGIT_RUN = re.compile(r"\d{12,14}")

# Fields whose values are human-entered free text where stray whitespace is
# expected to be meaningless; everything string-typed gets the check.
_SKIP_WHITESPACE_FIELDS = {"_row"}


class LintFlag(BaseModel):
    code: str
    row: int
    field: str
    value: str
    message: str


def _whitespace_dirty(text: str) -> bool:
    return text != clean_ws(text)


def lint_style_number(row: dict, brand_key: str, brands: dict, flags: list[LintFlag]) -> None:
    value = row.get("style_number")
    if value in (None, ""):
        return
    text = str(value)
    row_no = int(row["_row"])
    ean = str(row.get("ean") or "")

    embedded = _DIGIT_RUN.findall(text)
    if embedded and ean:
        acceptable = {ean, "0" + ean}
        if not any(e in acceptable for e in embedded):
            flags.append(
                LintFlag(
                    code="STYLE_NUMBER_FOREIGN",
                    row=row_no,
                    field="style_number",
                    value=text,
                    message=f"embedded EAN {embedded[0]} does not match row EAN {ean}",
                )
            )

    row_brand = clean_ws(str(row.get("brand") or "")).casefold()
    for other_key, cfg in brands.items():
        prefix = cfg.get("style_prefix")
        if not prefix or not text.startswith(prefix):
            continue
        display = str(cfg.get("display_name", other_key)).casefold()
        if other_key != brand_key and row_brand not in (other_key, display):
            flags.append(
                LintFlag(
                    code="STYLE_NUMBER_FOREIGN",
                    row=row_no,
                    field="style_number",
                    value=text,
                    message=f"style prefix {prefix!r} belongs to brand {other_key!r}, "
                    f"not this sheet's brand {brand_key!r}",
                )
            )


def lint_row(row: dict, brand_key: str, brands: dict, rules: dict) -> list[LintFlag]:
    flags: list[LintFlag] = []
    row_no = int(row["_row"])

    for field, value in row.items():
        if field in _SKIP_WHITESPACE_FIELDS or not isinstance(value, str):
            continue
        if _whitespace_dirty(value):
            flags.append(
                LintFlag(
                    code="WHITESPACE",
                    row=row_no,
                    field=field,
                    value=value,
                    message="non-breaking space or leading/trailing whitespace",
                )
            )

    lint_style_number(row, brand_key, brands, flags)

    size = row.get("size")
    if size not in (None, "") and not check_size_format(str(size), rules):
        normalized = normalize_size(size)
        hint = f' (should be "{normalized}")' if normalized else ""
        flags.append(
            LintFlag(
                code="SIZE_FORMAT",
                row=row_no,
                field="size",
                value=str(size),
                message=f"size not in guide format{hint}",
            )
        )

    category = row.get("category")
    if category not in (None, "") and not check_category(str(category), rules):
        flags.append(
            LintFlag(
                code="CATEGORY_ENUM",
                row=row_no,
                field="category",
                value=str(category),
                message="not a whitelisted Boozt Product Category (enums fail closed)",
            )
        )

    return flags


def lint_sheet(rows: list[dict], brand_key: str, brands: dict, rules: dict) -> list[LintFlag]:
    flags: list[LintFlag] = []
    for row in rows:
        flags.extend(lint_row(row, brand_key, brands, rules))
    return flags


def lint_no_color_conventions(
    color_names_by_sheet: dict[str, list[object]], rules: dict
) -> list[LintFlag]:
    """Regression case 5: flag when more than one no-color convention is in
    use across the given sheets (CLEAR vs NO COLOR vs NO SHADE)."""
    aliases = {a.casefold() for a in rules["no_color_aliases"]}
    used: dict[str, set[str]] = {}
    for sheet, values in color_names_by_sheet.items():
        for value in values:
            if value in (None, ""):
                continue
            cleaned = clean_ws(str(value))
            if cleaned.casefold() in aliases:
                used.setdefault(cleaned.upper(), set()).add(sheet)

    if len(used) <= 1:
        return []
    detail = "; ".join(
        f"{alias} in {', '.join(sorted(sheets))}" for alias, sheets in sorted(used.items())
    )
    return [
        LintFlag(
            code="NO_COLOR_CONVENTION",
            row=0,
            field="color_name",
            value=", ".join(sorted(used)),
            message=f"inconsistent no-color conventions across sheets: {detail} "
            "(open question 3: standardize on one)",
        )
    ]
