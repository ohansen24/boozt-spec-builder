"""Brand-family fallback (Oli refinement point 4): rows the primary Shopify
storefront can't resolve are retried against regional sibling storefronts
(brand_family_domains, e.g. marianila.se) — brand authority, better than
retailer-primary. Confirmed live: marianila.se resolved 17/31 of the
marianila.com-unresolved OR26BZNX0001 EANs.
"""


import bsb.resolve.adapters.shopify as shopify_mod
from bsb.ingest.odm import OdmParseResult, OdmRow
from bsb.resolve.adapters.shopify import ShopifyVariantHit
from bsb.resolve.orchestrator import _wrap_shopify_hit, resolve_order_shopify


def _row(ean):
    return OdmRow(
        row_number=1, ean12=ean, gtin13="0" + ean, base_name="", shade=None, hints={}
    )


_INCI_HTML = (
    "<p>Ingredients: Aqua/Water, Cetearyl Alcohol, Behentrimonium Chloride, Glycerin, "
    "Argania Spinosa Kernel Oil, Panthenol, Parfum, Citric Acid, Limonene, Linalool, "
    "Phenoxyethanol, Benzyl Salicylate</p>"
)


def _hit(ean, title):
    return ShopifyVariantHit(
        gtin13="0" + ean, ean12=ean, ok=True, url=f"https://x/{ean}",
        product_url=f"https://x/products/{title.lower()}", product_title=title,
        barcode=ean, body_html=_INCI_HTML,
    )


def _miss(ean):
    return ShopifyVariantHit(
        gtin13="0" + ean, ean12=ean, ok=False, url="", reject_reason="barcode not in catalog"
    )


class _StubAdapter:
    """Resolves only the EANs in `known`; everything else misses."""

    def __init__(self, known: dict[str, str]):
        self.known = known
        self.fetcher = object()
        self.ean_cache = object()

    def resolve_variant(self, gtin13):
        ean = gtin13[1:] if gtin13.startswith("0") else gtin13
        return _hit(ean, self.known[ean]) if ean in self.known else _miss(ean)


def test_wrap_shopify_hit_marks_brand_family_source():
    row = _row("7391681036178")
    master, variant, _shade, has_inci = _wrap_shopify_hit(
        row, _hit(row.ean12, "Pure Volume Mousse"), {"display_name": "Maria Nila"}, "marianila.se"
    )
    assert "brand-family marianila.se" in variant.snippet
    assert master.product_name
    assert has_inci  # INCI pulled from body_html


def test_fallback_resolves_via_family_domain(monkeypatch):
    primary = _StubAdapter({"1111": "On Com"})           # resolves EAN 1111
    family = _StubAdapter({"2222": "Only On SE"})        # resolves EAN 2222

    # the fallback builds ShopifyAdapter(fetcher, fam_cfg, ean_cache) internally
    monkeypatch.setattr(shopify_mod, "ShopifyAdapter", lambda *a, **k: family)

    odm = OdmParseResult(
        rows=[_row("1111"), _row("2222"), _row("3333")],  # 3333 resolves nowhere
        header_row=1, order_number="OR26BZNX0001", issues=[], length_profile={},
    )
    brand_cfg = {"display_name": "Maria Nila", "brand_family_domains": ["marianila.se"]}
    res = resolve_order_shopify(odm, primary, brand_cfg)

    assert res.by_ean["1111"].ok  # primary
    assert res.by_ean["2222"].ok  # recovered via family domain
    assert "brand-family marianila.se" in res.by_ean["2222"].variant.snippet
    assert not res.by_ean["3333"].ok  # unresolved everywhere
    assert any("3333" in f for f in res.master_failures)


def test_no_family_domains_is_plain_primary(monkeypatch):
    primary = _StubAdapter({"1111": "On Com"})
    # if the fallback were entered it would need ShopifyAdapter; assert it isn't
    monkeypatch.setattr(
        shopify_mod, "ShopifyAdapter",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not build family adapter")),
    )
    odm = OdmParseResult(
        rows=[_row("1111"), _row("9999")], header_row=1,
        order_number="O", issues=[], length_profile={},
    )
    res = resolve_order_shopify(odm, primary, {"display_name": "Maria Nila"})
    assert res.by_ean["1111"].ok
    assert not res.by_ean["9999"].ok
