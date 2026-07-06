"""INCI extraction gaps found from live Maria Nila PDPs (Oli diagnostic
2026-07-06). Fixtures are real page slices under tests/fixtures/inci/.

Three gaps, all on GTIN-anchored brand PDPs that visibly list INCI:
  1. source-selection — INCI lives in a PDP accordion, not the Shopify
     body_html the adapter read (EAN 7391681036031).
  2. aerosol lead — dry shampoos legitimately open with a propellant
     (Butane/Hydrofluorocarbon), which the leading-ingredient whitelist
     rejected; and an inline <style> block after the list leaked CSS into
     the parsed segment.
  3. non-whitelist lead — silicone oils open with Cyclomethicone, powders
     with a starch; an explicit "Ingredients:" label is authoritative, so
     the lead whitelist is skipped for labeled blocks.
"""

from pathlib import Path

from bsb.extract.inci import extract_inci_from_html, inci_plausible

FIX = Path(__file__).parent / "fixtures" / "inci"


def _fixture(name):
    return (FIX / name).read_text()


def test_accordion_source_selection():
    got = extract_inci_from_html(_fixture("mn_shampoo_accordion.html"))
    assert got is not None
    assert got.text.startswith("Aqua/Water/Eau, Sodium Lauroyl Methyl Isethionate")
    assert "Glycerin" in got.text


def test_aerosol_propellant_lead_and_inline_css():
    got = extract_inci_from_html(_fixture("mn_drysham_aerosol.html"))
    assert got is not None
    assert got.text.startswith("Butane, Alcohol Denat., Isobutane, Propane")
    # the inline <style>/CSS that follows the list must NOT bleed into the value
    assert "@media" not in got.text
    assert "{" not in got.text and "px" not in got.text.split("Parfum")[-1][:40]


def test_non_whitelist_silicone_lead():
    got = extract_inci_from_html(_fixture("mn_argan_oil.html"))
    assert got is not None
    assert got.text.startswith("Cyclomethicone, Dimethiconol, Argania Spinosa")


def test_labeled_relaxes_lead_whitelist_but_keeps_guards():
    # a non-whitelist lead (silicone oil) is fine WHEN labeled …
    oil = "Cyclomethicone, Dimethiconol, Argania Spinosa Kernel Oil, Silica, Parfum, Limonene"
    assert inci_plausible(oil, labeled=True)[0]
    # … but not treated as an INCI list when unlabeled — guessing an unlabeled
    # block needs the lead whitelist to avoid grabbing arbitrary comma text
    assert not inci_plausible(oil, labeled=False)[0]
    # structural guards still bite even when labeled: marketing prose rejected
    prose = (
        "Helps your skin feel amazing, apply daily for best results, discover radiance now, "
        "formulated to deeply nourish and protect, leaves skin visibly soft and smooth"
    )
    assert not inci_plausible(prose, labeled=True)[0]
    # and mid-list truncation
    assert not inci_plausible("Aqua, Glycerin, Parfum, Limonene, Linalool,", labeled=True)[0]


def test_propellant_leads_pass_unlabeled_but_silicone_needs_label():
    # aerosol propellants ARE in the widened whitelist -> pass even unlabeled
    assert inci_plausible(
        "Hydrofluorocarbon 152a, Isobutane, Alcohol Denat., Rice Starch, Butane, Silica"
    )[0]
    assert inci_plausible("Butane, Isobutane, Propane, Alcohol Denat., Silica, Parfum")[0]
    # silicone-/starch-led lists are NOT whitelisted -> recovered only via the
    # explicit-label relaxation, never as an unlabeled guess
    silicone = (
        "Cyclomethicone, Dimethiconol, Argania Spinosa Kernel Oil, "
        "Crambe Abyssinica Seed Oil, Tocopherol, Parfum"
    )
    assert not inci_plausible(silicone)[0]
    assert inci_plausible(silicone, labeled=True)[0]
