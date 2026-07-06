"""Coverage improvement levers (Oli 2026-07-06), reliability-preserving.

Lever 1: brand-PDP per-variant volume (selectedVariationAttributes.size
.description) -> size at brand authority, no full/mini guessing.
"""

from bsb.resolve.adapters.sfcc_catalog import SfccCatalogAdapter


def _pv(size_desc=None, size_label="Full Size"):
    size_vals = [{"value": size_label, "displayValue": size_label, "selected": True}]
    if size_desc is not None:
        size_vals[0]["description"] = size_desc
    return {
        "id": "X",
        "selectedVariationAttributes": ({"size": {"description": size_desc}} if size_desc else {}),
        "variationAttributes": [{"id": "size", "values": size_vals}],
    }


def test_size_volume_prefers_metric_from_description():
    sv = SfccCatalogAdapter._size_volume
    assert sv(_pv("5.0 mL / 0.17 US fl. oz.")) == "5.0 ml"
    assert sv(_pv("8.5 g / 0.3 oz.")) == "8.5 g"
    assert sv(_pv("147 mL")) == "147 ml"
    # brand's own small net weight for a fine pencil — authoritative, kept
    assert sv(_pv("0.08 g Net wt. 0.002 oz.")) == "0.08 g"
    # falls back through the values[] description when selectedVariationAttributes absent
    p = {"variationAttributes": [{"id": "size", "values": [
        {"value": "Full Size", "selected": True, "description": "30 mL / 1.0 fl oz"}]}]}
    assert sv(p) == "30 ml"


def test_size_volume_none_when_no_metric():
    sv = SfccCatalogAdapter._size_volume
    assert sv(_pv(None)) is None            # no description
    assert sv(_pv("One size")) is None       # no metric token
    assert sv({"variationAttributes": []}) is None  # no size axis


def test_size_volume_ignores_oz_only():
    # US-only description with no metric -> None (never guess a metric)
    assert SfccCatalogAdapter._size_volume(_pv("0.17 US fl. oz.")) is None


# ---- Lever 2: shared retailer INCI/size builders (reliability surface) ------

from datetime import UTC, datetime  # noqa: E402

from bsb.models import SourceRef  # noqa: E402
from bsb.pipeline import build_retailer_inci_field, build_retailer_size_field  # noqa: E402
from bsb.resolve.generic import ResolverHit  # noqa: E402

INCI = "Aqua, Glycerin, Parfum, Limonene, Linalool"


def _ref(h):
    return SourceRef(url=h.url, method="dom", fetched_at=datetime.now(UTC), snippet=h.family)


def _h(family, market, inci=None, size=None, name="Product"):
    return ResolverHit(url=f"https://{family}/p", family=family, gtin_anchored=True,
                       market=market, inci=inci, size=size, name=name)


def test_inci_builder_single_eu_yellow():
    fv = build_retailer_inci_field([_h("cosmeterie", "EU", INCI)], _ref)
    assert fv.status == "SINGLE_SOURCE" and "single EU/UK" in fv.notes and fv.value


def test_inci_builder_two_families_green():
    fv = build_retailer_inci_field([_h("cosmeterie", "EU", INCI), _h("galeria", "EU", INCI)], _ref)
    assert fv.status == "VERIFIED" and "two families agree" in fv.notes


def test_inci_builder_non_eu_only_yellow_caveat():
    us = [_h("bluemercury", "US", INCI), _h("jomashop", "US", INCI)]
    fv = build_retailer_inci_field(us, _ref)
    assert fv.status == "SINGLE_SOURCE"  # never green on non-EU agreement alone
    assert "non-EU market source" in fv.notes and "additional allergens" in fv.notes


def test_inci_builder_none_without_inci():
    assert build_retailer_inci_field([_h("x", "EU", inci=None)], _ref) is None


def test_size_builder_from_field_and_title():
    assert build_retailer_size_field([_h("x", "EU", size="50 ml")], _ref).value == "50 ml"
    # harvested from the title when no explicit size field
    fv = build_retailer_size_field([_h("x", "EU", name="Cool Cream, 300 ml")], _ref)
    assert fv.value == "300 ml" and fv.status == "SINGLE_SOURCE"
    assert build_retailer_size_field([_h("x", "EU", name="no size")], _ref) is None
