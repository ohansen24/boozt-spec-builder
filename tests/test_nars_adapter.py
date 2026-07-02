"""NARS adapter contract tests against recorded live payloads (build kit
section 8: recorded HTML fixtures, no network in CI)."""

import gzip
from datetime import UTC, datetime

import pytest
from tests.conftest import FIXTURES

from bsb.extract.structured import (
    extract_json_object,
    jsonld_selected_shade,
    parse_jsonld_products,
    parse_sfcc_product_state,
)
from bsb.fetch.cache import CachedFetch, EanCache, HttpCache
from bsb.fetch.ladder import BotShell, PoliteFetcher
from bsb.normalize.boozt import normalize_size
from bsb.resolve.adapters.nars import NarsAdapter, extract_inci

NARS_FIX = FIXTURES / "nars"
MASTER = "999NAC0000192"
CONTROLLER = "https://www.narscosmetics.eu/on/demandware.store/Sites-nars_eu-Site/default/"
BRAND_CFG = {"controller_base": CONTROLLER, "adapter": "nars_sfcc"}


def _load(name: str) -> str:
    return gzip.decompress((NARS_FIX / f"{name}.html.gz").read_bytes()).decode("utf-8")


@pytest.fixture(scope="module")
def pdp() -> str:
    return _load("pdp_0194251140407")


@pytest.fixture(scope="module")
def partial_414() -> str:
    return _load("variation_0194251140414")


def test_pdp_product_state(pdp):
    state = parse_sfcc_product_state(pdp)
    assert state["ID"] == "0194251140407"
    assert state["masterID"] == MASTER
    assert len(state["variants"]) == 31
    assert state["variants"]["color-0194251140414"]["attributes"]["color"] == "DEEP THROAT – 237"


def test_partial_product_state_uses_pdpdata_anchor(partial_414):
    state = parse_sfcc_product_state(partial_414)
    assert state["ID"] == "0194251140414"  # the requested variant, not the referer's
    assert state["masterID"] == MASTER


def test_jsonld_selected_shade(pdp):
    products = parse_jsonld_products(pdp)
    assert jsonld_selected_shade(products) == "ORGASM – 777"


def test_extract_inci(pdp):
    inci = extract_inci(pdp)
    assert inci is not None
    assert inci.startswith("SYNTHETIC FLUORPHLOGOPITE")
    assert "MAY CONTAIN" in inci
    assert "may evolve" not in inci  # disclaimer paragraph excluded


def test_extract_json_object_handles_braces_in_strings():
    text = 'var x = {"a": "value with } brace", "b": {"c": 1}} tail'
    assert extract_json_object(text, "var x =") == {"a": "value with } brace", "b": {"c": 1}}
    assert extract_json_object(text, "var missing =") is None


class FixtureFetcher:
    """Serves recorded payloads by URL; counts calls. No network."""

    def __init__(self, responses: dict[str, tuple[str, str]]):
        self.responses = responses  # url -> (final_url, text)
        self.calls: list[str] = []

    def get(self, url, *, referer=None, ajax=False, use_cache=True, validator=None):
        self.calls.append(url)
        final_url, text = self.responses[url]
        if validator is not None and not validator(text):
            raise BotShell(f"{url}: failed validation", None)
        return CachedFetch(
            url=url,
            final_url=final_url,
            status=200,
            text=text,
            fetched_at=datetime.now(UTC),
        )


SHOW_URL = f"{CONTROLLER}Product-Show?pid=0194251140407"
PDP_URL = "https://www.narscosmetics.eu/en/powder-blush/0194251140407.html"
VAR_URL_414 = (
    f"{CONTROLLER}Product-Variation?pid={MASTER}"
    f"&dwvar_{MASTER}_color=0194251140414&Quantity=1&format=ajax"
)
VAR_URL_407 = (
    f"{CONTROLLER}Product-Variation?pid={MASTER}"
    f"&dwvar_{MASTER}_color=0194251140407&Quantity=1&format=ajax"
)


@pytest.fixture()
def adapter(tmp_path, pdp, partial_414):
    fetcher = FixtureFetcher(
        {
            SHOW_URL: (PDP_URL, pdp),
            VAR_URL_414: (VAR_URL_414, partial_414),
            VAR_URL_407: (VAR_URL_407, _load("variation_0194251140407")),
        }
    )
    return NarsAdapter(fetcher, BRAND_CFG, EanCache(tmp_path)), fetcher


def test_discover_master_via_product_show(adapter):
    nars, fetcher = adapter
    master = nars.discover_master("0194251140407")
    assert master.master_id == MASTER
    assert master.product_name == "POWDER BLUSH"
    assert master.pdp_url == PDP_URL  # final URL after the 301, as provenance
    assert master.shade_by_gtin["0194251140407"] == "ORGASM – 777"
    assert len(master.shade_by_gtin) == 31
    assert master.size_text == "4.8g"
    assert master.inci_selected_gtin == "0194251140407"
    assert fetcher.calls == [SHOW_URL]


def test_resolve_variant_gtin_anchor_ok(adapter, tmp_path):
    nars, _ = adapter
    master = nars.discover_master("0194251140407")
    variant = nars.resolve_variant(master, "0194251140414")
    assert variant.ok
    assert variant.returned_id == "0194251140414"
    assert variant.shade == "DEEP THROAT – 237"
    assert variant.product_name == "POWDER BLUSH"
    assert variant.size_text == "4.8g"
    assert variant.ean12 == "194251140414"
    # resolved record cached under cache/eans/{gtin13}.json
    cached = EanCache(tmp_path).read("0194251140414")
    assert cached["shade"] == "DEEP THROAT – 237"
    assert cached["source_url"] == VAR_URL_414


def test_resolve_variant_rejects_wrong_gtin(adapter, tmp_path, partial_414):
    """GTIN-anchor rule: if the controller returns a different variant than
    requested, the payload is rejected, never adopted."""
    nars, fetcher = adapter
    master = nars.discover_master("0194251140407")
    wrong_url = (
        f"{CONTROLLER}Product-Variation?pid={MASTER}"
        f"&dwvar_{MASTER}_color=0194251149999&Quantity=1&format=ajax"
    )
    fetcher.responses[wrong_url] = (wrong_url, partial_414)  # returns 414 payload
    variant = nars.resolve_variant(master, "0194251149999")
    assert not variant.ok
    assert "returned ID '0194251140414'" in variant.reject_reason
    assert EanCache(tmp_path).read("0194251149999") is None  # nothing cached


def test_size_normalizes_to_guide_format():
    assert normalize_size("4.8g") == "4.8 g"


def test_polite_fetcher_cache_first(tmp_path):
    import httpx

    hits = {"n": 0}

    def handler(request):
        hits["n"] += 1
        return httpx.Response(200, text='var productCache = {"ID": "x"};')

    fetcher = PoliteFetcher(
        HttpCache(tmp_path),
        min_interval=0.0,
        transport=httpx.MockTransport(handler),
    )
    a = fetcher.get("https://example.test/p")
    b = fetcher.get("https://example.test/p")
    assert hits["n"] == 1
    assert not a.from_cache and b.from_cache


def test_polite_fetcher_backoff_and_stop_loss(tmp_path):
    import httpx

    def always_503(request):
        return httpx.Response(503, text="")

    fetcher = PoliteFetcher(
        HttpCache(tmp_path),
        min_interval=0.0,
        max_retries=2,
        stop_loss=2,
        transport=httpx.MockTransport(always_503),
    )
    import time

    t0 = time.monotonic()
    with pytest.raises(Exception, match="giving up"):
        fetcher.get("https://example.test/a")
    assert time.monotonic() - t0 >= 2.0  # exponential backoff slept

    from bsb.fetch.ladder import HostStopLoss

    with pytest.raises(HostStopLoss):
        fetcher.get("https://example.test/b")
