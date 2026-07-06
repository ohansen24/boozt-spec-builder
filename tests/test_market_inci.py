"""Market-aware INCI (Oli refinement 2026-07-06): GTIN anchors identity
country-agnostically, but INCI must be EU-registered. Prefer EU/UK sources; a
non-EU list ships yellow with an allergen caveat and never greens on non-EU
agreement alone. Names/sizes carry no market restriction.
"""

from bsb.ingest.odm import OdmRow
from bsb.models import ProductRecord
from bsb.pipeline import apply_retailer_primary
from bsb.resolve.generic import ResolverHit
from bsb.resolve.market import classify_market, is_eu_market

INCI = "Aqua, Glycerin, Parfum, Limonene"


def _row():
    return OdmRow(
        row_number=1, ean12="7391681111111", gtin13="07391681111111",
        base_name="", shade=None, hints={},
    )


def _rec():
    r = _row()
    return ProductRecord(ean12=r.ean12, gtin13=r.gtin13, brand="Maria Nila")


def _hit(family, market, inci=INCI, name="Shampoo"):
    return ResolverHit(
        url=f"https://{family}.example/p", family=family, gtin_anchored=True,
        market=market, name=name, inci=inci,
    )


# ---- classifier ----------------------------------------------------------

def test_classify_market_examples():
    assert classify_market("https://www.cosmeterie.com/x") == "EU"       # known FR
    assert classify_market("https://www.lookfantastic.com/x") == "UK"    # known UK
    assert classify_market("https://rozetka.pl/123") == "EU"             # PL ccTLD
    assert classify_market("https://rozetka.com.ua/ua/x") == "OTHER"     # UA — not EU
    assert classify_market("https://www.bluemercury.com/x") == "US"      # known US
    assert classify_market("https://shop.example.de/x") == "EU"          # DE ccTLD
    assert classify_market("https://brand.com/en-gb/p") == "UK"          # path locale
    assert classify_market("https://brand.com/en-us/p") == "US"          # path locale
    assert classify_market("https://unknown-shop.com/p") == "OTHER"      # conservative
    assert is_eu_market("EU") and is_eu_market("UK")
    assert not is_eu_market("US") and not is_eu_market("OTHER") and not is_eu_market(None)


# ---- retailer-primary INCI market gate -----------------------------------

def test_two_non_eu_families_never_green(brands, rules):
    """Agreement among non-EU sources must NOT green — EU list may add allergens."""
    rec = _rec()
    hits = [_hit("bluemercury", "US"), _hit("jomashop", "US")]  # identical INCI, both US
    apply_retailer_primary(rec, _row(), hits, brands["maria_nila"], rules)
    assert rec.ingredients.value  # value still shipped
    assert rec.ingredients.status == "SINGLE_SOURCE"  # NOT verified
    assert "non-EU market source" in rec.ingredients.notes
    assert "additional allergens" in rec.ingredients.notes


def test_eu_family_agreement_greens(brands, rules):
    rec = _rec()
    hits = [_hit("cosmeterie", "EU"), _hit("haarshop", "EU")]  # two EU families agree
    apply_retailer_primary(rec, _row(), hits, brands["maria_nila"], rules)
    assert rec.ingredients.status == "VERIFIED"
    assert "EU-sourced" in rec.ingredients.notes


def test_single_eu_family_yellow_no_caveat(brands, rules):
    rec = _rec()
    apply_retailer_primary(rec, _row(), [_hit("cosmeterie", "EU")], brands["maria_nila"], rules)
    assert rec.ingredients.status == "SINGLE_SOURCE"
    assert "single EU/UK retailer family" in rec.ingredients.notes
    assert "additional allergens" not in rec.ingredients.notes  # EU: no caveat


def test_eu_preferred_over_non_eu_when_both_present(brands, rules):
    """An EU source present -> it is the shipped primary even if a US hit came
    first in the list; a US hit agreeing corroborates the EU value to green."""
    rec = _rec()
    hits = [_hit("bluemercury", "US"), _hit("cosmeterie", "EU")]  # US first, EU second
    apply_retailer_primary(rec, _row(), hits, brands["maria_nila"], rules)
    assert rec.ingredients.status == "VERIFIED"
    assert "cosmeterie[EU]" in rec.ingredients.notes  # EU is primary


def test_names_carry_no_market_restriction(brands, rules):
    """Sanity: a US-only family still fills the NAME (identity is GTIN-anchored,
    country-agnostic) — the market gate is INCI-only."""
    rec = _rec()
    apply_retailer_primary(rec, _row(), [_hit("bluemercury", "US")], brands["maria_nila"], rules)
    assert rec.style_name.value  # name filled from the US family
