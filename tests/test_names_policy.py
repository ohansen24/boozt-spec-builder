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


# ---- Benefit numbered-shade normalization (Oli 2026-07-06, confirmed) -----

def test_benefit_shade_format_keeps_number_drops_separator():
    from bsb.normalize.boozt import normalize_color_name
    cfg = {"shade_format": {"drop_leading_number_separator": True, "title_case": True}}
    cases = {
        "3.5 - Neutral medium brown": "3.5 Neutral Medium Brown",  # number kept, sep dropped
        "2-Best Life (Fair Warm)": "2 Best Life (Fair Warm)",      # no-space sep + parenthetical
        "7-Jump In (Medium-Tan Warm)": "7 Jump In (Medium-Tan Warm)",  # internal hyphen kept
        "5 - Warm black-brown": "5 Warm Black-Brown",              # internal hyphen kept
        "Aurora (fair light pink)": "Aurora (Fair Light Pink)",    # no number, parenthetical tc
        "Golden brick red": "Golden Brick Red",
        "Clear": "Clear",
        "Hoola": "Hoola",
    }
    for raw, want in cases.items():
        assert normalize_color_name(raw, cfg) == want, raw


def test_benefit_number_is_not_stripped():
    # the NARS strip must NOT apply — the number is the shade identity
    from bsb.normalize.boozt import normalize_color_name
    cfg = {"shade_format": {"drop_leading_number_separator": True, "title_case": True}}
    out = normalize_color_name("3.5 - Neutral medium brown", cfg)
    assert out.startswith("3.5 ")  # number retained


def test_leading_numeric_separator_qa_flag():
    from bsb.validate.language import leading_numeric_separator
    assert leading_numeric_separator("3.5 - Neutral medium brown")  # raw -> flag
    assert leading_numeric_separator("2 - Warm golden blonde")
    assert not leading_numeric_separator("3.5 Neutral Medium Brown")  # canonical -> clean
    assert not leading_numeric_separator("Golden Brick Red")
    assert not leading_numeric_separator("Clear")


# ---- caps-guard: preserve deliberate mixed-case; title-case site styling ----

def test_caps_guard_preserves_identity_casing():
    from bsb.normalize.boozt import normalize_color_name
    b = {"shade_format": {"drop_leading_number_separator": True, "title_case": True}}
    assert normalize_color_name("22 Silk PJs (Rich Plum)", b) == "22 Silk PJs (Rich Plum)"
    assert normalize_color_name("McBride Rose", b) == "McBride Rose"
    # uniform UPPER / lower is site styling -> title-cased
    nars = {"shade_format": {"strip_shade_codes": True, "title_case": True}}
    assert normalize_color_name("DOLCE VITA", nars) == "Dolce Vita"
    assert normalize_color_name("ORGASM - 777", nars) == "Orgasm"  # strip flow unchanged
    # Laguna override path untouched (returns template, never title-cased)
    over = {
        "shade_format": {"strip_shade_codes": True, "title_case": True},
        "shade_format_overrides": {
            "laguna bronzing powder": {"number_template": "Laguna {number:02d}"}
        },
    }
    got = normalize_color_name("LAGUNA 05", over, product_name="Laguna Bronzing Powder")
    assert got == "Laguna 05"


def test_caps_qa_flag_is_source_based():
    from bsb.validate.language import caps_review_tokens
    # short uniformly-UPPER SOURCE token -> flag (ambiguous styling/initialism)
    assert caps_review_tokens("XX Volumising Mascara") == ["XX"]
    # deliberate mixed-case is identity -> never flagged
    assert caps_review_tokens("Silk PJs") == []
    # normal short words (title/mixed in source) -> not flagged (avoids the
    # 'My'/'Fan'/'Wax' false positives an output-based check produced)
    assert caps_review_tokens("Precisely, My Brow Pencil") == []
    assert caps_review_tokens("Neutral Medium Brown") == []
