"""INCI authority + order-neutral comparison + folding + lint hardening
(Oli 2026-07). The root fix: a retailer INCI must never DELETE the brand's own
authoritative EU-registered list. It may confirm it (->green) or annotate it
(->yellow note), never demote it to red. Plus the comparator is made robust to
the benign retailer variations that were false-firing "conflicts":
alphabetization, German/localised spellings, spacing/hyphenation, and generic
fragrance naming — while genuine ingredient differences still surface.

Fixtures are the real diffs seen on live Maria Nila retailer PDPs (OR26BZNX0001).
"""

from datetime import UTC, datetime

from bsb.extract.inci import inci_plausible
from bsb.ingest.odm import OdmRow
from bsb.models import ProductRecord
from bsb.pipeline import apply_resolution
from bsb.resolve.adapters.sfcc import MasterResult, VariantResult
from bsb.resolve.market import inci_authority
from bsb.resolve.orchestrator import ResolvedEan
from bsb.validate.matrix import compare_inci, is_alphabetized

# ---- R2/R3: order-neutral compare + alphabetization + synonym folding -------


def test_alphabetized_retailer_is_content_identical():
    # bellaffair dry-shampoo: same formula, sorted A-Z, "Parfum" vs
    # "Parfum/Fragrance", "(Potato)" parenthetical dropped
    brand = ("Butane, Alcohol Denat., Isobutane, Propane, Aluminum Starch Octenylsuccinate, "
             "Solanum Tuberosum (Potato) Starch, Silica, Parfum/Fragrance")
    retailer = ("Alcohol denat., Aluminum Starch Octenylsuccinate, Butane, Isobutane, "
                "Parfum, Propane, Silica, Solanum Tuberosum Starch")
    assert compare_inci(brand, retailer)[0] == "identical"
    assert is_alphabetized(retailer)
    assert not is_alphabetized(brand)  # brand keeps descending-weight order


def test_german_localised_spellings_fold_to_identical():
    # cosmeterie (DE): propandiol/butan/isobutan/annus + PVP<->Polyvinylpyrrolidon
    brand = ("Aqua/Water/Eau, Butane, Isobutane, Propanediol, "
             "Helianthus Annuus (Sunflower) Seed Extract, PVP")
    retailer = ("Aqua, Butan, Isobutan, Propandiol, Helianthus Annus Seed Extract, "
                "Polyvinylpyrrolidon")
    assert compare_inci(brand, retailer)[0] == "identical"


def test_spacing_and_hyphen_insensitive():
    a = "Aqua, Cetearyl Alcohol, Alpha-Isomethyl Ionone, Quaternium-95"
    b = "Aqua, Cetearylalcohol, Alphaisomethyl Ionone, Quaternium95"
    assert compare_inci(a, b)[0] == "identical"


def test_genuine_ingredient_difference_still_surfaces():
    # a real extra/missing ingredient is NOT swallowed (Oli R5): the brand names
    # a specific fragrance component, the retailer only "Parfum"
    verdict, diff = compare_inci(
        "Oryza Sativa Starch, Silica, Rose Ketones",
        "Oryza Sativa Starch, Silica, Parfum/Fragrance",
    )
    assert verdict == "base_diff"
    assert "rose ketones" in diff and "parfum" in diff


def test_distinct_ingredients_stay_distinct():
    # Glycerin != Glycerine (not in the synonym table — a deliberate difference)
    assert compare_inci("Aqua, Glycerin", "Aqua, Glycerine")[0] == "base_diff"
    # a real swap
    assert compare_inci("Talc, Mica, Zinc Stearate",
                        "Talc, Dimethicone, Zinc Stearate")[0] == "base_diff"


def test_short_lists_not_flagged_alphabetized():
    assert not is_alphabetized("Aqua, Glycerin")  # < 4 tokens: coincidence guard


# ---- R1: authority hierarchy -------------------------------------------------


def test_inci_authority_ranks():
    assert inci_authority(None, is_brand=True) == 4
    assert inci_authority("EU") == 3 == inci_authority("UK")
    assert inci_authority("US") == 2 == inci_authority("OTHER")
    assert inci_authority("EU", is_weak=True) == 1


# ---- R1/R5: pipeline — retailer never deletes brand INCI --------------------


def _resolved_with_brand_inci(ean, brand_inci):
    row = OdmRow(row_number=1, ean12=ean, gtin13="0" + ean, base_name="", shade=None, hints={})
    rec = ProductRecord(ean12=ean, gtin13="0" + ean, brand="Maria Nila")
    master = MasterResult(
        master_id="M", product_name="True Soft Shampoo", pdp_url="https://marianila.com/p",
        discovered_via_gtin="0" + ean, selected_id="V", size_text="350 ml", region="EU",
        inci_text=brand_inci, inci_selected_gtin="0" + ean, fetched_at=datetime.now(UTC),
    )
    variant = VariantResult(gtin13="0" + ean, ean12=ean, ok=True, master_id="M",
                            url="https://marianila.com/p", product_name="True Soft Shampoo",
                            size_text="350 ml")
    resolved = ResolvedEan(ean12=ean, gtin13="0" + ean, ok=True, master=master, variant=variant)
    return rec, row, resolved


BRAND_INCI = "Aqua/Water/Eau, Cetearyl Alcohol, Argania Spinosa Kernel Oil, Glycerin, Parfum"


def test_disagreeing_retailer_annotates_never_deletes(brands, rules):
    rec, row, resolved = _resolved_with_brand_inci("7391681403017", BRAND_INCI)
    # a lower-authority retailer with a genuinely different base list
    retailer = ("Aqua, Amodimethicone, Dimethicone, Polyglyceryl-3 Polyricinoleate, "
                "Butyrospermum Parkii Butter, Trideceth-5", "https://cosmeterie.com/p", "EU")
    apply_resolution(rec, row, resolved, brands["maria_nila"], rules, retailer_inci=retailer)
    # brand value KEPT, downgraded to yellow with a visible annotation — NEVER red/empty
    assert rec.ingredients.value is not None
    assert rec.ingredients.value.startswith("Aqua/Water/Eau, Cetearyl Alcohol")
    assert rec.ingredients.status == "SINGLE_SOURCE"
    assert "brand authoritative" in rec.ingredients.notes
    assert "retailer base list differs" in rec.ingredients.notes


def test_confirming_retailer_greens(brands, rules):
    # 8-token brand list so the A-Z detector's coincidence floor (>=8) applies
    brand8 = ("Aqua/Water/Eau, Cetearyl Alcohol, Glycerin, Parfum, Butane, "
              "Isobutane, Propane, Silica")
    rec, row, resolved = _resolved_with_brand_inci("7391681403017", brand8)
    # alphabetized (A-Z) retailer with the SAME content confirms to green
    retailer = ("Aqua, Butane, Cetearyl Alcohol, Glycerin, Isobutane, Parfum, Propane, Silica",
                "https://bellaffair.com/p", "EU")
    apply_resolution(rec, row, resolved, brands["maria_nila"], rules, retailer_inci=retailer)
    assert rec.ingredients.status == "VERIFIED"
    assert rec.ingredients.value.startswith("Aqua/Water/Eau, Cetearyl Alcohol")
    assert "content-identical" in rec.ingredients.notes
    assert "A-Z ordered" in rec.ingredients.notes  # order came from the brand, noted


# ---- R4: lint rejects leaked page furniture ---------------------------------

_PAD = "Aqua, Glycerin, Cetearyl Alcohol, Behentrimonium Chloride, Cetyl Alcohol, "


def test_lint_rejects_leaked_page_furniture():
    garbage = {
        "disclaimer": _PAD + "disclaimer our suppliers apply the ingredients of sometimes add "
                             "their products in between so check the packaging, Parfum",
        "marketing": _PAD + "keeping your hair conditioned wheat protein retains moisture for a "
                            "long-lasting conditioning effect sunflower oil creates a barrier, X",
        "see_full": _PAD + "see full ingredients list, Parfum, Limonene",
        "concentration": _PAD + "water 52 0 sodium-lauroyl-sarcosinate 24 0 propanediol, Parfum",
        "n_ingredients": "26 ingredients aqua, glycerin, cetearyl alcohol, parfum, limonene, x",
    }
    for name, text in garbage.items():
        ok, _ = inci_plausible(text, labeled=True)
        assert not ok, f"{name} should be rejected"
    # a clean list still passes
    assert inci_plausible(_PAD + "Parfum, Limonene, Linalool", labeled=True)[0]


def test_disclaimer_trailer_trimmed_not_dropped():
    # Benefit SFCC accordions append a legal disclaimer after the list; the
    # trailer is CUT (content-preserving), keeping the INCI + may-contain block,
    # so the value is corrected rather than dropped by the lint (Oli R4).
    from bsb.extract.inci import strip_inci_trailer

    raw = ("Aqua (water), butylene glycol, silica, simethicone. "
           "[+/- Ci 77491, ci 77891 (titanium dioxide)]. "
           "Disclaimer: Product ingredient listings are updated periodically. "
           "Before using a benefit product, please read the ingredient list on the packaging.")
    cleaned = strip_inci_trailer(raw)
    assert cleaned.endswith("(titanium dioxide)]")
    assert "disclaimer" not in cleaned.lower() and "packaging" not in cleaned.lower()
    assert inci_plausible(cleaned, labeled=True)[0]
    # a clean list with no trailer is returned unchanged
    clean = "Aqua, Glycerin, Cetearyl Alcohol, Parfum, Limonene, Linalool"
    assert strip_inci_trailer(clean) == clean


def test_inci_blocklist_loads_from_validators_yaml():
    from pathlib import Path

    from bsb.config import load_inci_blocklist

    bl = load_inci_blocklist(Path("config"))
    assert {"incibeauty", "world", "bluemercury", "salontotal"} <= bl


# ---- review-hardening regressions (adversarial review 2026-07) --------------


def test_strip_trailer_is_content_preserving_all_positions():
    from bsb.extract.inci import strip_inci_trailer
    # (a) leading preamble: keep the list that FOLLOWS, drop the preamble
    a = strip_inci_trailer("Product ingredient listings are updated periodically. "
                           "Aqua · Glycerin · Cetearyl Alcohol · Parfum · Ci 77891")
    assert "Aqua" in a and "Glycerin" in a and "periodically" not in a
    # (b) mid-list note before may-contain: keep BOTH sides, incl. colorants
    b = strip_inci_trailer("Aqua · Glycerin · Cetearyl Alcohol · Parfum. Before using please read "
                           "the ingredient list on the packaging. May Contain: Ci 77891 · Ci 77491")
    assert "Ci 77891" in b and "Ci 77491" in b and "please" not in b.lower()
    # (c) trailing disclaimer: keep list + may-contain, drop the trailer; an
    # all-lowercase multi-word ingredient must NOT be mistaken for prose
    c = strip_inci_trailer("Aqua, ammonium acrylates copolymer, silica. [+/- Ci 77891 "
                           "(titanium dioxide)]. Disclaimer: Product ingredient listings updated.")
    assert "ammonium acrylates copolymer" in c and "titanium dioxide" in c
    assert "disclaimer" not in c.lower()


def test_lint_keeps_ci_group_and_multilingual_token():
    # space-joined 5-digit Colour Index codes are a legit colorant group, NOT a
    # concentration table
    assert inci_plausible("Aqua, Glycerin, Talc, Mica, CI 77491 77492 77499, CI 77891",
                          labeled=True)[0]
    # a concentration column ("52 0 … 24 0") is still rejected
    assert not inci_plausible("Aqua, Glycerin, Talc, water 52 0 sodium x 24 0 propanediol, Parfum",
                              labeled=True)[0]
    # a spaced multilingual name is one ingredient, not an over-long prose token
    assert inci_plausible("Aqua / Water / Eau / Wasser / Acqua / Agua / Vand, Glycerin, "
                          "Cetearyl Alcohol, Parfum, Limonene", labeled=True)[0]


def test_distinct_colorants_with_ci_parens_not_merged():
    # a numeric (CI) parenthetical is IDENTITY, not a clarifier — keep it
    assert compare_inci("Aqua, Iron Oxides (CI 77491), Talc",
                        "Aqua, Iron Oxides (CI 77499), Talc")[0] == "base_diff"
    # a digit-free clarifier still folds (omitted common name)
    assert compare_inci("Butyrospermum Parkii (Shea) Butter, Aqua",
                        "Butyrospermum Parkii Butter, Aqua")[0] == "identical"


def test_retailer_primary_name_strips_brand_and_size():
    # a retailer title "Brand - Product - 250 ml" must ship as the bare product
    # name, not carry the brand prefix or size (which has its own field)
    from bsb.validate.matrix import clean_retail_name
    assert clean_retail_name("Maria Nila - Shaping Heat Spray - 250 ml", "Maria Nila") \
        == "Shaping Heat Spray"
    assert clean_retail_name("Purifying Cleanse Shampoo 1000ml", "Maria Nila") \
        == "Purifying Cleanse Shampoo"
    assert clean_retail_name("Maria Nila Purifying Cleanse Shampoo, 350 ml", "Maria Nila") \
        == "Purifying Cleanse Shampoo"
    # an internal dash that is part of the real name is preserved
    assert clean_retail_name("Maria Nila True Soft - Argan Oil 100 ml", "Maria Nila") \
        == "True Soft - Argan Oil"


def test_equal_authority_retailer_disagreement_fails_closed():
    # no brand list; two EU retailer families genuinely disagree -> red (R1/R5),
    # not a silent first-wins single-source
    from datetime import UTC, datetime

    from bsb.models import SourceRef
    from bsb.pipeline import build_retailer_inci_field
    from bsb.resolve.generic import ResolverHit

    def ref(h):
        return SourceRef(url=h.url, method="dom", fetched_at=datetime.now(UTC), snippet=h.family)

    def h(family, inci):
        return ResolverHit(url=f"https://{family}/p", family=family, gtin_anchored=True,
                           market="EU", inci=inci)
    hits = [h("cosmeterie", "Aqua, Glycerin, Niacinamide, Phenoxyethanol"),
            h("haarshop", "Aqua, Glycerin, Phenoxyethanol")]
    fv = build_retailer_inci_field(hits, ref)
    assert fv.status == "CONFLICT" and fv.value is None
    assert "disagree" in fv.notes and "fail closed" in fv.notes
    # but two AGREEING EU families still green
    ok = build_retailer_inci_field([h("cosmeterie", "Aqua, Glycerin, Phenoxyethanol"),
                                    h("haarshop", "Aqua, Glycerin, Phenoxyethanol")], ref)
    assert ok.status == "VERIFIED"
