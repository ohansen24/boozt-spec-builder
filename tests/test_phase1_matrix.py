"""Phase 1 pure logic: shade normalization, validation matrix, INCI compare,
LF payload parsing (recorded fixture), orchestrator flow with a fake adapter."""

import gzip
from datetime import UTC, datetime

import pytest
from tests.conftest import FIXTURES

from bsb.ingest.odm import OdmParseResult, OdmRow
from bsb.models import SourceRef
from bsb.normalize.boozt import normalize_color_name, normalize_style_name
from bsb.resolve.adapters.nars import MasterResult, VariantResult
from bsb.resolve.orchestrator import resolve_order
from bsb.validate.matrix import (
    clean_retail_name,
    combine_exact,
    compare_inci,
    confirm_name,
    odm_name_check,
    split_inci,
)


def _ref(url: str) -> SourceRef:
    return SourceRef(url=url, method="dom", fetched_at=datetime.now(UTC))


NARS_CFG = {
    "display_name": "NARS",
    "shade_format": {"strip_numeric_suffix": True, "title_case": True},
    "name_format": {"title_case": True},
}


class TestShadeNormalization:
    def test_strip_suffix_and_title_case(self):
        assert normalize_color_name("ORGASM – 777", NARS_CFG) == "Orgasm"
        assert normalize_color_name("DEEP THROAT – 237", NARS_CFG) == "Deep Throat"
        assert normalize_color_name("CAFÉ CON LECHE – 154", NARS_CFG) == "Café Con Leche"

    def test_no_suffix_survives(self):
        assert normalize_color_name("ORGASM X", NARS_CFG) == "Orgasm X"
        assert normalize_color_name("LAGUNA 01", NARS_CFG) == "Laguna 01"  # no dash: kept

    def test_without_brand_config_verbatim(self):
        assert normalize_color_name("ORGASM – 777") == "ORGASM – 777"
        assert normalize_color_name(" Orgasm ") == "Orgasm"

    def test_style_name_title_case(self):
        assert normalize_style_name("POWDER BLUSH", NARS_CFG) == "Powder Blush"
        assert (
            normalize_style_name("NATURAL RADIANT LONGWEAR FOUNDATION", NARS_CFG)
            == "Natural Radiant Longwear Foundation"
        )


class TestMatrix:
    def test_two_families_agree_verified(self):
        fv = combine_exact("shade", "Orgasm", _ref("a"), "Orgasm", _ref("b"))
        assert fv.status == "VERIFIED"
        assert fv.value == "Orgasm"
        assert fv.secondary is not None

    def test_single_family_yellow(self):
        fv = combine_exact("shade", "Orgasm", _ref("a"), None, None)
        assert fv.status == "SINGLE_SOURCE"

    def test_disagreement_conflict_with_both_urls(self):
        fv = combine_exact("shade", "Orgasm", _ref("url-a"), "Dominate", _ref("url-b"))
        assert fv.status == "CONFLICT"
        assert fv.value is None  # conflicted value never ships
        assert "url-a" in fv.notes and "url-b" in fv.notes

    def test_missing_both_not_found(self):
        fv = combine_exact("size", None, None, None, None)
        assert fv.status == "NOT_FOUND"

    def test_confirm_name_threshold(self):
        good = confirm_name(
            "Powder Blush",
            _ref("a"),
            "NARS Blush 4.8g (Various Shades)",
            _ref("b"),
            brand="NARS",
        )
        assert good.status == "VERIFIED"
        bad = confirm_name(
            "Powder Blush", _ref("a"), "NARS Radiant Creamy Concealer 6ml", _ref("b"), brand="NARS"
        )
        assert bad.status == "SINGLE_SOURCE"  # never a conflict, retailer styling
        assert "too different" in bad.notes

    def test_clean_retail_name(self):
        assert (
            clean_retail_name("NARS Blush 4.8g (Various Shades) Thrill", "NARS", ["Thrill"])
            == "Blush"
        )

    def test_odm_name_check_notes_on_distance(self):
        note = odm_name_check("Powder Blush", "Orgasm", "Talc-Free Blush - Orgasm")
        assert note is None or "ODM calls this" in note  # close enough either way
        far = odm_name_check("Powder Blush", "Orgasm", "Radiant Creamy Concealer - Custard")
        assert far is not None


class TestInciCompare:
    A = "TALC · MICA · ZINC STEARATE · [+/-(MAY CONTAIN/PEUT CONTENIR): CI 77491 · MICA]"

    def test_identical(self):
        assert compare_inci(self.A, self.A) == ("identical", "")

    def test_may_contain_only_diff(self):
        b = "TALC · MICA · ZINC STEARATE · [+/-(MAY CONTAIN/PEUT CONTENIR): CI 77491]"
        verdict, diff = compare_inci(self.A, b)
        assert verdict == "may_contain_diff"
        assert "mica" in diff

    def test_base_diff(self):
        b = "TALC · DIMETHICONE · ZINC STEARATE · [+/-(MAY CONTAIN/PEUT CONTENIR): CI 77491 · MICA]"
        verdict, diff = compare_inci(self.A, b)
        assert verdict == "base_diff"
        assert "mica" in diff and "dimethicone" in diff

    def test_split_handles_comma_style(self):
        base, may = split_inci("Water, Talc, May Contain/Peut Contenir/(+/-): Ci 77491")
        assert base == ["water", "talc"]
        assert may == ["ci 77491"]


@pytest.fixture(scope="module")
def lf_html() -> str:
    return gzip.decompress((FIXTURES / "nars" / "lf_nars_blush.html.gz").read_bytes()).decode(
        "utf-8"
    )


class TestLfParser:
    def test_variation_data_parsed(self, lf_html):
        from bsb.resolve.validators import LookfantasticValidator

        product = LookfantasticValidator._parse_product(
            object.__new__(LookfantasticValidator), lf_html, "https://lf/p/x", False
        )
        assert product is not None
        assert "194251140407" in product.by_barcode
        assert product.by_barcode["194251140407"].shade == "Orgasm"
        assert product.size_text == "4.8g"
        assert "NARS Blush" in product.product_name


def _master(shades: dict[str, str]) -> MasterResult:
    return MasterResult(
        master_id="999NAC0000192",
        product_name="POWDER BLUSH",
        pdp_url="https://nars/pdp",
        discovered_via_gtin="0194251140407",
        selected_id="0194251140407",
        selected_shade="ORGASM – 777",
        shade_by_gtin=shades,
        color_val_by_gtin={g: g for g in shades},
        size_text="4.8g",
    )


class FakeAdapter:
    def __init__(
        self,
        master: MasterResult,
        reject: set[str] | None = None,
        own_pdp: dict[str, MasterResult] | None = None,
    ):
        self.master = master
        self.reject = reject or set()
        self.own_pdp = own_pdp or {}

    def discover_master(self, gtin13: str) -> MasterResult:
        return self.own_pdp.get(gtin13, self.master)

    def variant_from_pdp(self, master: MasterResult) -> VariantResult:
        return VariantResult(
            gtin13=master.selected_id,
            ean12=master.selected_id[1:],
            ok=True,
            master_id=master.master_id,
            url=master.pdp_url,
            returned_id=master.selected_id,
            shade=master.selected_shade,
            product_name=master.product_name,
            size_text=master.size_text,
            snippet=f'"ID":"{master.selected_id}" (PDP product-state self-anchor)',
        )

    def resolve_variant(self, master: MasterResult, gtin13: str) -> VariantResult:
        ean12 = gtin13[1:]
        if gtin13 in self.reject:
            return VariantResult(
                gtin13=gtin13,
                ean12=ean12,
                ok=False,
                master_id=master.master_id,
                url="u",
                returned_id="0000000000000",
                reject_reason=(
                    f"variation partial returned ID '0000000000000', requested {gtin13!r}"
                ),
            )
        return VariantResult(
            gtin13=gtin13,
            ean12=ean12,
            ok=True,
            master_id=master.master_id,
            url="u",
            returned_id=gtin13,
            shade=master.shade_by_gtin.get(gtin13),
            product_name=master.product_name,
            size_text=master.size_text,
        )


def _odm(rows: list[OdmRow]) -> OdmParseResult:
    return OdmParseResult(rows=rows, header_row=1)


def _row(ean12: str, name: str) -> OdmRow:
    base, _, shade = name.partition(" - ")
    return OdmRow(
        row_number=2,
        ean12=ean12,
        gtin13="0" + ean12,
        base_name=base,
        shade=shade or None,
        hints={"name": name},
    )


class TestOrchestrator:
    def test_happy_path(self):
        master = _master({"0194251140407": "ORGASM – 777", "0194251140414": "DEEP THROAT – 237"})
        odm = _odm(
            [_row("194251140407", "Blush - Orgasm"), _row("194251140414", "Blush - Deep Throat")]
        )
        result = resolve_order(odm, FakeAdapter(master))
        assert result.counts()["resolved_ok"] == 2
        assert not result.blocking_anomalies

    def test_anchor_rejection_is_blocking(self):
        master = _master({"0194251140407": "ORGASM – 777", "0194251140414": "DEEP THROAT – 237"})
        odm = _odm([_row("194251140414", "Blush - Deep Throat")])
        result = resolve_order(odm, FakeAdapter(master, reject={"0194251140414"}))
        assert result.anchor_rejections
        assert result.blocking_anomalies

    def test_missing_shade_is_blocking(self):
        master = _master({"0194251140407": "ORGASM – 777"})
        odm = _odm([_row("194251149999", "Blush - Mystery")])
        result = resolve_order(odm, FakeAdapter(master))
        assert result.missing_shades
        assert result.blocking_anomalies

    def test_simple_product_pdp_anchors_itself(self):
        master = _master({})
        master.master_id = "0194251140407"
        odm = _odm([_row("194251140407", "Light Reflecting Mist")])
        result = resolve_order(odm, FakeAdapter(master))
        entry = result.by_ean["194251140407"]
        assert entry.ok
        assert entry.variant.snippet.endswith("(PDP product-state self-anchor)")
        assert not result.blocking_anomalies

    def test_delisted_shade_falls_back_to_own_pdp(self):
        """A shade missing from the master swatch list but whose own PDP
        self-anchors (live case: LRF Gobi) resolves with a warning, not a
        blocking anomaly."""
        master = _master({"0194251140407": "ORGASM – 777"})
        gobi_pdp = _master({"0194251140407": "ORGASM – 777"})
        gobi_pdp.selected_id = "0194251070421"
        gobi_pdp.selected_shade = "GOBI"
        gobi_pdp.pdp_url = "https://nars/gobi"
        odm = _odm([_row("194251070421", "Light Reflecting Foundation - Gobi")])
        result = resolve_order(odm, FakeAdapter(master, own_pdp={"0194251070421": gobi_pdp}))
        entry = result.by_ean["194251070421"]
        assert entry.ok
        assert entry.variant.shade == "GOBI"
        assert result.swatch_warnings and "delisted" in result.swatch_warnings[0]
        assert not result.blocking_anomalies


def test_validator_only_value_ships_yellow():
    """Kit 6.5: one family = yellow. A GTIN-anchored retailer may carry a
    field alone when the brand page lacks it (live case: delisted Gobi)."""
    from bsb.validate.matrix import combine_exact

    fv = combine_exact("shade", None, None, "Gobi", _ref("https://lf/p/x"))
    assert fv.status == "SINGLE_SOURCE"
    assert fv.value == "Gobi"
    assert fv.primary.url == "https://lf/p/x"


    def test_semi_delisted_shade_uses_own_pdp(self):
        """In the variants map but missing from the purchasable vals (live
        case: the Orgasm quad) — resolved via own PDP, warned, not blocked."""
        master = _master({"0194251140407": "ORGASM – 777"})
        master.shade_by_gtin["0194251026404"] = "Orgasm"  # listed…
        # …but absent from color_val_by_gtin (no purchasable val)
        own = _master({})
        own.selected_id = "0194251026404"
        own.selected_shade = "Orgasm"
        odm = _odm([_row("194251026404", "Eyeshadow Quad - Orgasm")])
        result = resolve_order(odm, FakeAdapter(master, own_pdp={"0194251026404": own}))
        entry = result.by_ean["194251026404"]
        assert entry.ok
        assert result.swatch_warnings and "not purchasable" in result.swatch_warnings[0]
        assert not result.blocking_anomalies
