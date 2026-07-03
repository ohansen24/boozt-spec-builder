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
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    from bsb.fetch.cache import HttpCache
    from bsb.fetch.firecrawl import FirecrawlClient
    from bsb.fetch.ladder import HostRateLimiter

    client = FirecrawlClient(HttpCache(tmp_path), HostRateLimiter(), api_key=None)
    assert not client.available
    import pytest

    from bsb.fetch.ladder import FetchError

    with pytest.raises(FetchError, match="FIRECRAWL_API_KEY"):
        client.search("test")
