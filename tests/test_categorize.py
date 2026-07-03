"""Categorization (build kit 6.7): fail closed, foundation-family first.
The trap: ~67 of OR26BZQN0001's rows are Foundation despite the ODM calling
them "Face Make-Up"."""

from collections import Counter

import pytest
from tests.conftest import ODM_PATH

from bsb.categorize.rules import categorize, color_code_for
from bsb.ingest.odm import parse_odm


@pytest.fixture(scope="module")
def odm():
    return parse_odm(ODM_PATH)


def test_foundation_family_trap_covers_67_rows(odm, rules, brands):
    decisions = [categorize(r.base_name, rules, brands["nars"]) for r in odm.rows]
    counts = Counter(d.category for d in decisions)
    assert counts["Foundation"] == 67  # foundations 31, concealers 26, tinted moisturizers 10


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Natural Radiant Longwear Foundation", "Foundation"),
        ("Light Reflecting Foundation", "Foundation"),
        ("Radiant Creamy Concealer", "Foundation"),
        ("Soft Matte Complete Concealer", "Foundation"),
        ("Mini Radiant Creamy Concealer", "Foundation"),
        ("Pure Radiant Tinted Moisturizer", "Foundation"),
        ("Laguna Bronzing Powder", "Makeup"),
        ("Talc-Free Blush", "Makeup"),
        ("Eyeshadow Quad", "Makeup"),
        ("Total Seduction Eyeshadow Stick", "Makeup"),
        ("Eyebrow Gel", "Makeup"),
        ("Climax Mascara", "Makeup"),
        ("Soft Matte Primer", "Makeup"),
        ("Afterglow Lip Shine", "Makeup"),
        ("Powermatte High Intensity Lip Pencil", "Makeup"),
        ("Light Reflecting Setting Powder", "Makeup"),
    ],
)
def test_category_decisions(rules, brands, name, expected):
    assert categorize(name, rules, brands["nars"]).category == expected


def test_multiple_is_brand_curated_makeup(rules, brands):
    decision = categorize("Multiple", rules, brands["nars"])
    assert decision.category == "Makeup"
    assert decision.rule == "brand:multiple"


def test_fail_closed_on_unknown(rules, brands):
    """Light Reflecting Mist has no rule -> stays empty and red. The tool
    never invents a category."""
    decision = categorize("Light Reflecting Mist", rules, brands["nars"])
    assert decision.category is None
    assert decision.rule is None


def test_odm_subcategory_never_consulted(rules, brands):
    """The categorizer signature takes only the product name and configs —
    "Face Make-Up" (the ODM's word for foundations) must not resolve."""
    assert categorize("Face Make-Up", rules, brands["nars"]).category is None


def test_keyword_needs_word_boundary(rules, brands):
    # "lip" must not fire inside other words
    assert categorize("Ellipse Cream", rules, brands["nars"]).category is None


def test_color_code_foundation_family_is_1018_confirmed(rules):
    decision = color_code_for("Foundation", "Barcelona", rules)
    assert decision.code == 1018
    assert not decision.pending_confirmation  # confirmed Felina 2026-07-03


def test_color_code_skincare_is_1017(rules):
    assert color_code_for("Skin care", None, rules).code == 1017
    assert color_code_for("Body Care", None, rules).code == 1017


def test_color_code_shade_lexicon(rules):
    assert color_code_for("Makeup", "Orgasm", rules).code == 1003
    assert color_code_for("Makeup", "Laguna 03", rules).code == 1010  # prefix entry
    assert color_code_for("Makeup", "Clear", rules).code == 1017
    # exact entries must not prefix-match: Orgasm X is NOT confirmed pink
    assert color_code_for("Makeup", "Orgasm X", rules).code is None


def test_color_code_fails_closed(rules):
    decision = color_code_for("Makeup", "Rebellion", rules)
    assert decision.code is None


def test_dg_trigger_categories_never_default_flammable(rules, brands):
    """Review finding B: DG rows stay red until a human confirms (kit 6.8)."""
    from datetime import UTC, datetime

    from bsb.ingest.odm import OdmRow
    from bsb.pipeline import build_record

    row = OdmRow(
        row_number=8,
        ean12="194251026404",
        gtin13="0194251026404",
        base_name="Nail Lacquer",
        shade="Big Apple Red",
        hints={"coo": "US", "price": 9.9},
    )
    record = build_record(row, "nars", brands, rules, "odm.xlsx", datetime.now(UTC))
    assert record.category.value == "Nail polish"
    assert record.flammable.value is None
    assert record.flammable.status == "NOT_FOUND"
    assert "DG-trigger" in record.flammable.notes
