"""Guide validations from build kit section 8: category enum, color enum,
size regex, 60-char name limit, GS1 check digit, EAN-13 leading-zero rule,
PG vs flash point table."""

import pytest

from bsb.normalize.boozt import clean_ws, normalize_category, normalize_size
from bsb.validate.guide import (
    check_category,
    check_color_code,
    check_ean_submission_form,
    check_name_length,
    check_pg_flash_point,
    check_size_format,
)


def test_category_enum_fails_closed(rules):
    assert check_category("Makeup", rules)
    assert check_category("Foundation", rules)
    assert not check_category("\xa0Makeup\xa0", rules)  # raw regression value
    assert not check_category("Foundations", rules)
    assert not check_category("Face Make-Up", rules)  # ODM subcategory is not the enum
    assert not check_category("", rules)


def test_category_normalization_recovers_canonical_form(rules):
    assert normalize_category("\xa0Makeup\xa0", rules) == "Makeup"
    assert normalize_category("body care", rules) == "Body Care"
    assert normalize_category("Face Make-Up", rules) is None  # never mapped, only cleaned


def test_color_code_enum(rules):
    assert len(rules["color_codes"]) == 22
    for code in (1001, 1017, 1018, 1022):
        assert check_color_code(code, rules)
        assert check_color_code(str(code), rules)
    assert not check_color_code(1000, rules)
    assert not check_color_code(1023, rules)
    assert not check_color_code("Pink", rules)
    assert not check_color_code(None, rules)


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("4.4 g", True),
        ("50 ml", True),
        ("50 pcs", True),
        ("One Size", True),
        ("4,4gr", False),
        ("4.4g", False),
        ("50 ML", False),
        ("50ml", False),
        ("ONE SIZE", False),
        ("4.4 gr", False),
    ],
)
def test_size_regex(rules, value, ok):
    assert check_size_format(value, rules) is ok


@pytest.mark.parametrize(
    ("raw", "unit", "expected"),
    [
        ("4,4gr", None, "4.4 g"),  # regression case 3
        ("4.4", "GR", "4.4 g"),  # ODM hint form
        ("50", "ML", "50 ml"),
        ("ONE SIZE", None, "One Size"),
        ("50.0", "ml", "50 ml"),
        ("about 50", "ml", None),  # unparseable fails closed
        (None, "ml", None),
    ],
)
def test_normalize_size(raw, unit, expected):
    assert normalize_size(raw, unit) == expected


def test_name_length_limit(rules):
    assert check_name_length("Natural Radiant Longwear Foundation", rules)
    assert not check_name_length("x" * 61, rules)
    assert check_name_length("x" * 60, rules)


def test_ean13_leading_zero_rule():
    """Boozt: an EAN-13 must not start with 0 — 12-digit UPCs are submitted
    as-is, matching the finished sheets."""
    assert check_ean_submission_form("194251026404")  # 12-digit UPC, as-is
    assert not check_ean_submission_form("0194251026404")  # zero-padded 13 -> refuse
    assert check_ean_submission_form("3614274581058")  # genuine EAN-13 (Aesop)
    assert not check_ean_submission_form("19425102640")  # 11 digits
    assert not check_ean_submission_form("194251O26404")  # letter O


@pytest.mark.parametrize(
    ("pg", "flash", "ok"),
    [
        ("II", 21.0, True),  # the Aesop DG anchor: PG 2, flash 21
        ("2", 21.0, True),
        ("PG II", 22.9, True),
        ("II", 23.0, False),
        ("III", 23.0, True),
        ("III", 61.0, True),
        ("III", 61.1, False),
        ("3", 15.0, False),
        ("I", 10.0, False),  # not in the guide table -> fail closed
    ],
)
def test_pg_vs_flash_point_table(rules, pg, flash, ok):
    assert check_pg_flash_point(pg, flash, rules) is ok


def test_clean_ws_strips_nbsp_and_edges():
    assert clean_ws("\xa0Makeup\xa0") == "Makeup"
    assert clean_ws("Orgasm ") == "Orgasm"
    assert clean_ws("Deep  Rose") == "Deep  Rose"  # interior spacing is preserved
