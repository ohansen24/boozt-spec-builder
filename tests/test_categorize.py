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


def test_color_code_shade_lexicon_is_brand_scoped(rules, brands):
    nars = brands["nars"]
    assert color_code_for("Makeup", "Orgasm", rules, nars).code == 1003
    assert color_code_for("Makeup", "Laguna 03", rules, nars).code == 1010  # prefix entry
    assert color_code_for("Makeup", "Clear", rules, nars).code == 1017
    # Orgasm X now has its OWN confirmed entry (Felina 2026-07-03)
    assert color_code_for("Makeup", "Orgasm X", rules, nars).code == 1003
    # exact entries still never prefix-match an unconfirmed variant
    assert color_code_for("Makeup", "Clear Glow", rules, nars).code is None
    # same shade name, different brand: no lexicon -> fail closed
    assert color_code_for("Makeup", "Orgasm", rules, brands["olaplex"]).code is None
    assert color_code_for("Makeup", "Orgasm", rules, None).code is None


def test_multi_shade_products_never_use_the_lexicon(rules, brands):
    """One shade name, several colors: the Orgasm QUAD must not inherit the
    Orgasm blush's 1003 — it takes the confirmed palette rule (1016)."""
    nars = brands["nars"]
    for name in ("Eyeshadow Quad", "Quad Eyeshadow", "Some Palette", "Cheek Trio", "Lip Duo"):
        decision = color_code_for("Makeup", "Orgasm", rules, nars, product_name=name)
        assert decision.code == 1016, name
        assert decision.rule == "multi_shade_default"  # never lexicon:orgasm
    # single-shade products with similar words are unaffected
    assert color_code_for("Makeup", "Orgasm", rules, nars, product_name="Powder Blush").code == 1003
    # "quadra"-like words must not trip the word-boundary marker
    assert (
        color_code_for("Makeup", "Orgasm", rules, nars, product_name="Quadrille Blush").code == 1003
    )


def test_color_code_fails_closed(rules, brands):
    # a shade in nobody's lexicon (Rebellion joined it via Felina's codes)
    decision = color_code_for("Makeup", "Zanzibar", rules, brands["nars"])
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


def test_benefit_site_category_map(rules, brands):
    """Benefit marketing names carry no category keyword — the first-party
    datalayer categoryID is the signal. concealer -> Foundation (=> 1018);
    shade makeup -> Makeup with color_code fail-closed; unmapped id fails
    closed even when a shade is present."""
    benefit = brands["benefit"]

    # marketing name that matches NO keyword rule, but categoryID does the work
    d = categorize("Shellie", rules, benefit, site_category_id="blush")
    assert d.category == "Makeup"
    assert d.rule == "site_category:blush"

    # foundation-trap: concealer -> Foundation -> 1018 (not fail-closed)
    d = categorize("Boi-ing Cakeless", rules, benefit, site_category_id="concealer")
    assert d.category == "Foundation"
    assert color_code_for(d.category, "2-Best Life", rules, benefit).code == 1018

    # shade makeup: category decided; color_code has no confirmed lexicon, so it
    # is a Stage-1 colour-word PROPOSAL (yellow) — "Dark Cherry" -> cherry ->
    # 1009 Red; a shade with no clear colour word still fails closed.
    d = categorize("Benetint", rules, benefit, site_category_id="liptint")
    assert d.category == "Makeup"
    cc = color_code_for(d.category, "Dark Cherry", rules, benefit)
    assert cc.code == 1009 and cc.proposal
    assert color_code_for(d.category, "Hoola", rules, benefit).code is None

    # skincare -> Skin care
    assert categorize("Good Cleanup", rules, benefit, site_category_id="cleanser").category == (
        "Skin care"
    )

    # unmapped categoryID fails closed (never guessed)
    assert categorize("Mystery Box", rules, benefit, site_category_id="giftset").category is None
    # no site id + no keyword -> fail closed
    assert categorize("Shellie", rules, benefit).category is None


def test_sfcc_catalog_extract_variants():
    """The analytics datalayer parse: upc + variant code + hex-from-image, and
    the product's own categoryID (not the nav-menu entries)."""
    from bsb.resolve.adapters.sfcc_catalog import SfccCatalogAdapter, _product_category

    html = (
        'prefix "variants":[{"name":"Boi-ing Cakeless",'
        '"image_url":"https://x/product_images/BOIINGHC/Large_f6dece_1_shade.jpg",'
        '"upc":"602004111548","page_id_variant":"FM188"},'
        '{"name":"Boi-ing Cakeless","image_url":"https://x/Large_eacfba_1.jpg",'
        '"upc":"602004111555","page_id_variant":"FM189"}] suffix'
    )
    adapter = SfccCatalogAdapter.__new__(SfccCatalogAdapter)  # no network
    entries = adapter.extract_variants(
        html, "https://b.com/en-gb/product/boi-ing-cakeless-concealer-BOIINGHC.html"
    )
    assert [e.upc for e in entries] == ["602004111548", "602004111555"]
    assert entries[0].variant_code == "FM188"
    assert entries[0].hex == "F6DECE"
    assert entries[0].master_code == "BOIINGHC"
    assert entries[0].gtin13 == "0602004111548"

    dec = (
        '{"products":[{"id":"BOIINGHC","name":"Boi-ing Cakeless",'
        '"category":"Concealer","categoryID":"concealer","price":"26.00"}]}'
        ',{"id":"GIMMEBROW","name":"Gimme Brow+","category":"Brow Gel & Wax",'
        '"categoryID":"browgelandwax"}'
    )
    assert _product_category(dec, "BOIINGHC") == ("Concealer", "concealer")
    assert _product_category(dec, "GIMMEBROW") == ("Brow Gel & Wax", "browgelandwax")
    assert _product_category(dec, "NOPE") == (None, None)
