"""The five observed manual errors from build kit section 1, replayed from
the real fixtures. Every one must be flagged — these are the regression cases
the tool exists to catch."""

import pytest
from tests.conftest import FIXTURES, TEMPLATE_PATH

from bsb.ingest.template import read_sheet_rows
from bsb.validate.lint import lint_no_color_conventions, lint_sheet


@pytest.fixture(scope="module")
def wip_flags(synonyms, brands, rules):
    rows = read_sheet_rows(FIXTURES / "nars_wip.xlsx", synonyms)
    return lint_sheet(rows, "nars", brands, rules)


def _flags(flags, code, field=None, row=None):
    return [
        f
        for f in flags
        if f.code == code and (field is None or f.field == field) and (row is None or f.row == row)
    ]


def test_case_1_svr_style_number_in_nars_order(wip_flags):
    """WIP row 2: Style number "SVR3662361001699" — SVR prefix plus a wrong
    EAN pasted into a NARS order. Both independent signals must fire."""
    flags = _flags(wip_flags, "STYLE_NUMBER_FOREIGN", field="style_number", row=2)
    messages = " | ".join(f.message for f in flags)
    assert any("3662361001699" in f.message for f in flags), messages
    assert any("'SVR'" in f.message for f in flags), messages


def test_case_2_nbsp_in_category(wip_flags):
    """WIP row 2: Boozt Product Category " Makeup " with non-breaking spaces:
    whitespace lint fires, and the raw value fails the closed enum."""
    assert _flags(wip_flags, "WHITESPACE", field="category", row=2)
    assert _flags(wip_flags, "CATEGORY_ENUM", field="category", row=2)


def test_case_3_size_comma_decimal_and_gr(wip_flags):
    """WIP row 2: Size "4,4gr" — comma decimal and unit gr instead of "4.4 g"."""
    flags = _flags(wip_flags, "SIZE_FORMAT", field="size", row=2)
    assert flags
    assert '"4.4 g"' in flags[0].message  # the fix is suggested, not silently applied


def test_case_4_trailing_space_in_color_name(wip_flags):
    """WIP row 2: Color Name "Orgasm " with a trailing space."""
    flags = _flags(wip_flags, "WHITESPACE", field="color_name", row=2)
    assert flags
    assert flags[0].value == "Orgasm "


def test_case_5_no_color_conventions_inconsistent_across_sheets(synonyms, rules):
    """CLEAR (SVR, Aesop) vs NO COLOR (Olaplex): the finished sheets disagree
    on the no-color convention (open question 3)."""
    sheets = {
        "svr_template": TEMPLATE_PATH,
        "aesop_final": FIXTURES / "aesop_final.xlsx",
        "olaplex_final": FIXTURES / "olaplex_final.xlsx",
    }
    color_names = {
        name: [row.get("color_name") for row in read_sheet_rows(path, synonyms)]
        for name, path in sheets.items()
    }
    flags = lint_no_color_conventions(color_names, rules)
    assert len(flags) == 1
    message = flags[0].message
    assert "CLEAR" in message and "NO COLOR" in message
    assert "olaplex_final" in message and "aesop_final" in message


def test_no_color_convention_single_alias_is_clean(rules):
    flags = lint_no_color_conventions({"a": ["CLEAR", "Clear"], "b": ["clear", None]}, rules)
    assert flags == []


def test_wip_has_no_false_positive_storm(wip_flags):
    """Only row 2 is substantially filled; the empty rows must not flood the
    report. Every flag should point at row 2."""
    assert {f.row for f in wip_flags} == {2}
