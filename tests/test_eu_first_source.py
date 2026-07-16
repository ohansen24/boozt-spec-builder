"""EU-market-first brand-site selection (Oli 2026-07-16). The EU regulates
cosmetic chemicals/colourants/additives more tightly than the US, so a brand's
EU site carries the fuller regulatory ingredient list. Proven live: marianila.de
matched the physical pack where marianila.com did not. Try .de first, then other
EU markets, .com last.
"""

import json

import pytest

from bsb.fetch.ladder import FetchError
from bsb.resolve.adapters.shopify import ShopifyAdapter
from bsb.resolve.market import MARKET_DOMAIN_PRIORITY, eu_first_domains


def test_eu_first_domains_orders_de_first_com_last():
    cands = eu_first_domains("marianila.com")
    assert cands[0] == "marianila.de"
    assert cands[-1] == "marianila.com"  # US is the last resort
    assert cands.index("marianila.fr") < cands.index("marianila.com")
    # every priority TLD is represented and the configured domain is present
    assert set(cands) >= {f"marianila.{t}" for t in ("de", "fr", "it", "es")}


def test_eu_first_domains_keeps_unusual_tld_as_fallback():
    cands = eu_first_domains("https://www.somebrand.io/")
    assert cands[0] == "somebrand.de"
    assert "somebrand.io" in cands  # configured domain always included


def test_priority_puts_germany_first_and_com_last():
    assert MARKET_DOMAIN_PRIORITY[0] == "de"
    assert MARKET_DOMAIN_PRIORITY[-1] == "com"


class _FakeFetch:
    def __init__(self, text):
        self.text = text


class _FakeFetcher:
    """Serves a Shopify products.json only for the domains in `live`."""

    def __init__(self, live: dict[str, int]):
        self.live = live  # domain -> product count
        self.gets: list[str] = []

    def get(self, url):
        self.gets.append(url)
        for dom, n in self.live.items():
            if f"//{dom}/" in url or url.startswith(f"https://{dom}"):
                return _FakeFetch(json.dumps({"products": [{"id": i} for i in range(n)]}))
        raise FetchError(f"{url}: unreachable")


def _adapter(live):
    a = ShopifyAdapter(_FakeFetcher(live), {"shopify": {"domain": "marianila.com"}}, ean_cache=None)
    a._select_base()
    return a


def test_select_base_prefers_de_when_it_resolves():
    a = _adapter({"marianila.de": 5, "marianila.com": 200})
    assert a.selected_domain == "marianila.de"
    assert a.base == "https://marianila.de"


def test_select_base_falls_through_eu_then_com():
    # no .de/.fr… only .com resolves -> .com is the last-resort fallback
    a = _adapter({"marianila.com": 200})
    assert a.selected_domain == "marianila.com"


def test_select_base_skips_empty_store_and_takes_next():
    # marianila.de resolves but has an EMPTY catalog (parked/placeholder) -> skip
    a = _adapter({"marianila.de": 0, "marianila.fr": 3, "marianila.com": 200})
    assert a.selected_domain == "marianila.fr"


def test_market_domains_override_wins():
    fetcher = _FakeFetcher({"marianila.se": 4, "marianila.com": 200})
    cfg = {
        "shopify": {"domain": "marianila.com"},
        "market_domains": ["marianila.se", "marianila.com"],
    }
    a = ShopifyAdapter(fetcher, cfg, ean_cache=None)
    a._select_base()
    assert a.selected_domain == "marianila.se"  # explicit list beats the TLD swap


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
