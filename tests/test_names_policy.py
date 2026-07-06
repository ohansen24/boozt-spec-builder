"""English-name policy + size-from-title harvest (Oli 2026-07-06). Boozt
requires English style_name/color_name; a non-English name is never shipped
(never translated) — fail closed when only non-English sources exist. Retail
titles embed sizes ("750 ml", "1000 мл") — harvest them, normalizing units.
"""

from bsb.ingest.odm import OdmRow
from bsb.models import ProductRecord
from bsb.pipeline import _size_from_title, apply_retailer_primary
from bsb.resolve.generic import ResolverHit
from bsb.validate.language import is_english_name, non_english_tokens


def _row(ean="7391681021105"):
    return OdmRow(
        row_number=1, ean12=ean, gtin13="0" + ean, base_name="", shade=None, hints={}
    )


def _rec(ean="7391681021105"):
    return ProductRecord(ean12=ean, gtin13="0" + ean, brand="Maria Nila")


def _hit(family, market, name, lang, color=None):
    return ResolverHit(
        url=f"https://{family}/p", family=family, gtin_anchored=True,
        market=market, name=name, color=color, language=lang,
    )


# ---- language module -----------------------------------------------------

def test_language_flags_polish_and_cyrillic():
    assert not is_english_name("Lotion do włosów neutralizując", "pl")
    assert non_english_tokens("Lotion do włosów")  # 'do'
    assert not is_english_name("Пінка для волосся", "uk")
    assert non_english_tokens("Пінка для волосся")  # non-Latin-script
    assert is_english_name("Structure Repair Holiday Box", "en")
    assert not non_english_tokens("Structure Repair Holiday Box")


# ---- fail-closed policy in retailer-primary ------------------------------

def test_non_english_only_name_fails_closed(brands, rules):
    rec = _rec()
    # only a Polish and a Ukrainian source — no English candidate
    hits = [
        _hit("rozetka.pl", "EU", "Lotion do włosów neutralizując", "pl"),
        _hit("rozetka.com.ua", "OTHER", "Пінка для волосся Maria Nila", "uk"),
    ]
    apply_retailer_primary(rec, _row(), hits, brands["maria_nila"], rules)
    assert rec.style_name.value is None
    assert rec.style_name.status == "NOT_FOUND"
    assert "only non-English sources found" in rec.style_name.notes


def test_english_source_ships_when_present(brands, rules):
    rec = _rec()
    hits = [
        _hit("rozetka.pl", "EU", "Lotion do włosów", "pl"),
        _hit("cosmeterie.com", "EU", "Maria Nila Cool Cream", "en"),
    ]
    apply_retailer_primary(rec, _row(), hits, brands["maria_nila"], rules)
    assert rec.style_name.value  # the English name shipped
    assert "włosów" not in (rec.style_name.value or "")


# ---- size harvested from retail titles -----------------------------------

def test_size_from_title_units():
    assert _size_from_title("Maria Nila Cool Cream, 300 ml") == "300 ml"
    assert _size_from_title("Пінка 1000 мл") == "1000 ml"   # Cyrillic ml -> ml
    assert _size_from_title("Dry Shampoo 250 гр") == "250 g"  # Cyrillic g -> g
    assert _size_from_title("no size here") is None


def test_size_harvested_from_title_when_no_size_field(brands, rules):
    rec = _rec()
    # English name, size only in the title, no explicit .size field on the hit
    hits = [_hit("cosmeterie.com", "EU", "Maria Nila Finishing Spray, 100 ml", "en")]
    apply_retailer_primary(rec, _row(), hits, brands["maria_nila"], rules)
    assert rec.size.value == "100 ml"
    assert rec.size.status == "SINGLE_SOURCE"
