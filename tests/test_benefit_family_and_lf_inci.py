"""Benefit catch-up (Oli 2026-07-06):
- SFCC brand-family shade recovery: a shade the primary (UK) site dropped is
  recovered from a regional sibling controller (DE) at brand authority.
- Lookfantastic INCI: LF serves INCI in a structured "ingredients" JSON field;
  find_product now extracts it so apply_resolution can ship retailer INCI for
  brands whose own PDPs carry none (Benefit).
"""

import json
from pathlib import Path

from bsb.resolve.adapters.sfcc_catalog import CatalogEntry, SfccCatalogAdapter
from bsb.resolve.validators import _lf_ingredients

FIX = Path(__file__).parent / "fixtures" / "inci"


def test_lf_ingredients_from_json_field():
    frag = (FIX / "lf_ingredients_json.txt").read_text()
    inci = _lf_ingredients(frag)
    assert inci is not None
    assert inci.startswith("Aqua (Water), Cera Alba (Beeswax), Paraffin")
    # bounded to the list — no trailing JSON/markup bleed
    assert "<p>" not in inci and '"' not in inci


def test_lf_ingredients_absent_returns_none():
    no_inci = '{"key":"howtouse","value":{"content":"<p>Wiggle the wand.</p>"}}'
    assert _lf_ingredients(no_inci) is None


class _StubFetch:
    def __init__(self, text):
        self.text = text
        self.fetched_at = None
        self.from_cache = True
        self.via = "httpx"


def _pv(variant_code, shade=None, size="Full Size"):
    color = {"id": "color", "values": []}
    if shade is not None:
        color["values"] = [{"value": "HEX", "displayValue": shade, "selected": True}]
    else:
        # color axis present but nothing selected (discontinued on this site)
        color["values"] = [{"value": "HEX", "displayValue": "Some Shade", "selected": False}]
    return json.dumps(
        {
            "product": {
                "id": variant_code,
                "productName": "Boi-ing Cakeless",
                "variationAttributes": [
                    {"id": "size", "values": [{"displayValue": size, "selected": True}]},
                    color,
                ],
            }
        }
    )


def test_brand_family_recovers_discontinued_shade(monkeypatch):
    cfg = {
        "controller_base": "https://x/Sites-benco-uk-Site/en_GB/",
        "catalog_sitemap": "https://x/sitemap.xml",
        "brand_family_controllers": [
            {"market": "DE", "controller_base": "https://x/Sites-benco-de-Site/de_DE/"}
        ],
    }
    adapter = SfccCatalogAdapter.__new__(SfccCatalogAdapter)
    adapter.controller_base = cfg["controller_base"]
    adapter.family_controllers = [
        {"market": "DE", "base": cfg["brand_family_controllers"][0]["controller_base"]}
    ]
    adapter.ean_cache = type("C", (), {"write": lambda *a, **k: None})()

    def fake_get(url, *, referer=None, ajax=False):
        # primary (UK) has no selected shade; DE controller does
        if "benco-de-Site" in url:
            return _StubFetch(_pv("FM193", shade="6-Fly High (Medium Cool)"))
        return _StubFetch(_pv("FM193", shade=None))

    monkeypatch.setattr(adapter, "_get", fake_get)

    entry = CatalogEntry(
        upc="602004111593", gtin13="0602004111593", master_code="BOIINGHC",
        master_pdp_url="https://x/p", variant_code="FM193", product_name="Boi-ing Cakeless",
    )
    result = adapter.resolve_variant(entry)
    assert result.ok
    assert result.shade == "6-Fly High (Medium Cool)"
    assert result.shade_unresolved is False
    assert "DE brand-family" in result.snippet


def test_no_family_controller_leaves_shade_unresolved(monkeypatch):
    adapter = SfccCatalogAdapter.__new__(SfccCatalogAdapter)
    adapter.controller_base = "https://x/Sites-benco-uk-Site/en_GB/"
    adapter.family_controllers = []  # no siblings configured
    adapter.ean_cache = type("C", (), {"write": lambda *a, **k: None})()
    monkeypatch.setattr(adapter, "_get", lambda url, **k: _StubFetch(_pv("FM193", shade=None)))
    entry = CatalogEntry(
        upc="602004111593", gtin13="0602004111593", master_code="BOIINGHC",
        master_pdp_url="https://x/p", variant_code="FM193", product_name="Boi-ing Cakeless",
    )
    result = adapter.resolve_variant(entry)
    assert result.ok and result.shade is None and result.shade_unresolved is True
