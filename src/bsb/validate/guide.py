"""Guide validations (build kit section 8): enum membership, size regex,
name length, GS1 check digit, EAN-13 leading-zero rule, PG vs flash point.
Each check returns True when the value is acceptable as-is for submission.
"""

import re

from bsb.ingest.odm import check_ean_submission_form, gs1_check_digit_ok

__all__ = [
    "check_category",
    "check_color_code",
    "check_ean_submission_form",
    "check_name_length",
    "check_pg_flash_point",
    "check_size_format",
    "gs1_check_digit_ok",
]


def check_category(value: str, rules: dict) -> bool:
    """Exact enum membership — raw value must already be canonical."""
    return value in rules["categories"]


def check_color_code(value: object, rules: dict) -> bool:
    try:
        code = int(str(value))
    except (TypeError, ValueError):
        return False
    return code in rules["color_codes"]


def check_size_format(value: str, rules: dict) -> bool:
    return re.match(rules["size_pattern"], value) is not None


def check_name_length(value: str, rules: dict) -> bool:
    return len(value) <= rules["name_max_chars"]


_PG_FORMS = {"2": "II", "II": "II", "3": "III", "III": "III"}


def check_pg_flash_point(packing_group: str, flash_point_c: float, rules: dict) -> bool:
    """Cross-validate packing group against flash point (guide DG table:
    PG II < 23C; PG III 23 to 61C). Accepts "II", "PG II", "2", "PG2"."""
    raw = packing_group.strip().upper().removeprefix("PG").strip()
    group = _PG_FORMS.get(raw)
    bounds = rules["pg_flash_point"].get(group) if group else None
    if bounds is None:
        return False
    if "max_exclusive" in bounds and not flash_point_c < bounds["max_exclusive"]:
        return False
    if "min_inclusive" in bounds and not flash_point_c >= bounds["min_inclusive"]:
        return False
    return not ("max_inclusive" in bounds and not flash_point_c <= bounds["max_inclusive"])
