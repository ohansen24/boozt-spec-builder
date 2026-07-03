"""Phase 2 platform capability: Shopify axes, generic resolver anchoring,
Firecrawl gating, probe drafts."""

from bsb.extract.structured import gtin_forms, page_asserts_gtin
from bsb.resolve.adapters.shopify import variant_axes
from bsb.resolve.generic import source_family


def test_gtin_forms():
    assert gtin_forms("0194251140407") == {"0194251140407", "194251140407"}
    assert gtin_forms("3614274581058") == {"3614274581058"}
    assert gtin_forms("850018802659") == {"850018802659", "0850018802659"}


def test_page_asserts_gtin_tiers():
    jsonld = (
        '<script type="application/ld+json">{"@type":"Product","gtin13":"3614274581058"}</script>'
    )
    assert page_asserts_gtin(jsonld, "3614274581058") == "jsonld:gtin13"
    micro = '<span itemprop="gtin13" content="3614274581058"></span>'
    assert page_asserts_gtin(micro, "3614274581058") == "microdata"
    assert page_asserts_gtin("<p>EAN 3614274581058</p>", "3614274581058") == "content"
    assert page_asserts_gtin("<p>x93614274581058</p>", "3614274581058") is None


def test_source_family_groups_tlds():
    assert source_family("https://www.narscosmetics.eu/en/x") == "narscosmetics"
    assert source_family("https://www.narscosmetics.com/USA/x") == "narscosmetics"
    assert source_family("https://www.lookfantastic.com/p/x") == "lookfantastic"


def test_variant_axes_both_shapes():
    # products.json shape
    product = {"options": [{"name": "Size"}, {"name": "Color"}]}
    variant = {"option1": "250ml", "option2": "Blonde"}
    assert variant_axes(product, variant) == {"Size": "250ml", "Color": "Blonde"}
    # {handle}.js shape
    product = {"options": ["Size"]}
    variant = {"options": ["100ml"]}
    assert variant_axes(product, variant) == {"Size": "100ml"}


def test_firecrawl_gated_without_key(tmp_path, monkeypatch):
    import bsb.fetch.firecrawl as fc

    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.setattr(fc, "load_env", lambda *a, **k: None)  # ignore repo .env
    from bsb.fetch.cache import HttpCache
    from bsb.fetch.firecrawl import FirecrawlClient
    from bsb.fetch.ladder import HostRateLimiter

    client = FirecrawlClient(HttpCache(tmp_path), HostRateLimiter(), api_key=None)
    assert not client.available
    import pytest

    from bsb.fetch.ladder import FetchError

    with pytest.raises(FetchError, match="FIRECRAWL_API_KEY"):
        client.search("test")


def test_firecrawl_key_prefix_normalized(tmp_path, monkeypatch):
    import bsb.fetch.firecrawl as fc

    monkeypatch.setattr(fc, "load_env", lambda *a, **k: None)
    from bsb.fetch.cache import HttpCache
    from bsb.fetch.firecrawl import FirecrawlClient
    from bsb.fetch.ladder import HostRateLimiter

    client = FirecrawlClient(HttpCache(tmp_path), HostRateLimiter(), api_key="abc123")
    assert client.api_key == "fc-abc123"
    client = FirecrawlClient(HttpCache(tmp_path), HostRateLimiter(), api_key="fc-abc123")
    assert client.api_key == "fc-abc123"


def test_inci_plausibility_lint():
    from bsb.extract.inci import inci_plausible

    good = (
        "Aqua, Glycerin, Niacinamide, Butylene Glycol, Dimethicone, "
        "Phenoxyethanol, Parfum, Citric Acid"
    )
    assert inci_plausible(good)[0]
    assert not inci_plausible("Aqua, Glycerin")[0]  # too few tokens
    marketing = (
        "This cream helps your skin feel great, apply daily for best results, "
        "enriched with vitamins, delivers hydration, use daily, discover more"
    )
    assert not inci_plausible(marketing)[0]
    assert not inci_plausible(good + ",")[0]  # truncated mid-list
    weird = "Sparkle Magic, Unicorn Dust, Niacinamide, Butylene Glycol, Dimethicone, Parfum"
    assert not inci_plausible(weird)[0]  # implausible lead


def test_inci_extraction_labeled_and_inline():
    from bsb.extract.inci import extract_inci_from_html

    labeled = """
    <div><h3>Ingredients</h3>
    <p>Aqua, Glycerin, Niacinamide, Butylene Glycol, Dimethicone, Phenoxyethanol,
    Parfum, Citric Acid, Sodium Hydroxide</p></div>
    <p>This cream helps your skin.</p>"""
    c = extract_inci_from_html(labeled)
    assert c is not None and c.source == "labeled-section"
    assert c.text.startswith("Aqua, Glycerin")

    inline = (
        "<div>Zusammensetzung: Aqua, Glycerin, Urea, Panthenol, Dimethicone, "
        "Parfum, Citric Acid How to use daily</div>"
    )
    c = extract_inci_from_html(inline)
    assert c is not None
    assert "Citric Acid" in c.text and "How to use" not in c.text

    assert extract_inci_from_html("<p>Great product, buy now!</p>") is None


def test_water_equivalence_compare_time_only():
    from bsb.validate.matrix import compare_inci

    a = "Water/Aqua/Eau, Glycerin, Niacinamide"
    b = "Aqua (Purified Water), Glycerin, Niacinamide"
    assert compare_inci(a, b) == ("identical", "")
    c = "Water, Glycerin, Niacinamide"
    assert compare_inci(a, c) == ("identical", "")
    # non-water tokens are never canonicalized
    assert compare_inci("Aqua, Glycerin", "Aqua, Glycerine")[0] == "base_diff"


def test_page_language_heuristics():
    from bsb.resolve.generic import page_language

    assert page_language("https://shop.example.de/p/x", "<html>") == "de"
    assert page_language("https://x.com/p", '<html lang="fr">') == "fr"
    assert page_language("https://x.com/p", "<html>") == "en"
