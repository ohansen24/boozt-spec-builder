"""INCI-driven flammability (Oli/Felina feedback, OR26BZNX0001, 2026-07-15).

Flammable is a formulation property, not a category one — an aerosol propellant
or volatile solvent in the ingredient list makes a product flammable regardless
of its Boozt category. Rule validated at 106/109 against Felina's hand
corrections; cases below are drawn from those rows. Felina's "anything with
Butane => flammable" is the headline subset; the rule also catches dimethyl
ether / alcohol denat. aerosols and, crucially, does NOT false-fire on fatty
alcohols (Cetearyl Alcohol) or a minor standalone ethanol in a leave-in.
"""

from pathlib import Path

from bsb.categorize.rules import flammable_from_inci
from bsb.config import load_rules

RULES = load_rules(Path("config"))


def _fl(inci):
    return flammable_from_inci(inci, RULES)[0]


def test_propellants_flag_flammable():
    # dry shampoo / hairspray / mousse
    assert _fl("Butane, Alcohol Denat., Isobutane, Propane, Aluminum Starch Octenylsuccinate, "
               "Solanum Tuberosum (Potato) Starch, Silica, Parfum/Fragrance") == "Yes"
    # dimethyl-ether spray with NO butane (Felina's butane rule alone would miss this)
    assert _fl("Aqua/Water/Eau, Dimethyl Ether, Alcohol Denat., VP/VA Copolymer, PVP") == "Yes"
    # hydrofluorocarbon dry shampoo
    assert _fl("Hydrofluorocarbon 152a, Isobutane, Propane, Alcohol Denat., Silica") == "Yes"


def test_fatty_alcohol_is_not_flammable():
    # conditioner: Cetearyl/Cetyl Alcohol are fatty alcohols, NOT flammable
    assert _fl("Aqua/Water/Eau, Cetearyl Alcohol, Behentrimonium Chloride, Cetyl Alcohol, "
               "Argania Spinosa Kernel Oil, Glycerin, Parfum") == "No"
    # a minor standalone Alcohol (ethanol) in a water-based leave-in -> not flammable
    # (Felina row 7391681038509)
    assert _fl("Aqua/Water/Eau, Cyclomethicone, Cetearyl Alcohol, Panthenol, Alcohol, "
               "Limonene, Linalool, Phenoxyethanol") == "No"


def test_localised_spellings_still_match():
    # Dutch retailer INCI: no-space compounds must still flag (Felina row 7391681404168)
    assert _fl("Dimethylether, alcoholdenat., propaandiol, VP/VA-copolymeer, PVP") == "Yes"


def test_no_inci_returns_unknown():
    assert flammable_from_inci(None, RULES) == (None, [])
    assert flammable_from_inci("", RULES) == (None, [])


def test_pipeline_flammable_overrides_category(brands, rules):
    # a "Hair care" mousse with butane must ship flammable=Yes, not the category
    # default "No" — via the shared _flammable_field builder
    from bsb.pipeline import _flammable_field
    fv = _flammable_field(
        "Alcohol Denat., Butane, Isobutane, Propane, Aqua, Parfum", "Hair care", rules
    )
    assert fv.value == "Yes" and "propellant" in fv.notes
    # same category, a plain conditioner -> No
    fv2 = _flammable_field("Aqua, Cetearyl Alcohol, Glycerin, Parfum", "Hair care", rules)
    assert fv2.value == "No"
    # a propellant with NO resolved category still flags Yes (orphan row)
    fv3 = _flammable_field("Butane, Isobutane, Propane, Alcohol Denat.", None, rules)
    assert fv3.value == "Yes"
    # DG-trigger category with no INCI -> SDS review (red), unchanged
    fv4 = _flammable_field(None, "Perfumes", rules)
    assert fv4.value is None and fv4.status == "NOT_FOUND"
