"""compare-external classification logic."""

from bsb.compare import _classify_values, norm_text


def test_norm_text_whitespace_case_decimal():
    assert norm_text(" 4,4\xa0g ") == "4.4 g"
    assert norm_text("Walk This Way") == norm_text("Walk this Way")
    assert norm_text(None) is None


def test_classify_tiers():
    assert _classify_values("color_name", "Orgasm ", "\xa0Orgasm") == ("AGREE", "")
    assert _classify_values("color_name", "Walk This Way", "Walk this Way")[0] == "FORMAT_ONLY"
    assert _classify_values("color_name", "Café Con Leche", "Cafe con Leche")[0] == "FORMAT_ONLY"
    assert _classify_values("color_name", "Explicit Black", "Black")[0] == "DISAGREE"
    assert _classify_values("color_name", "Laguna 01", "Laguna 02")[0] == "DISAGREE"
    assert _classify_values("size", "4.4 g", "4,4g") == ("AGREE", "")
    assert _classify_values("size", "6 g", "6 ml")[0] == "DISAGREE"
    assert _classify_values("color_code", "1018", 1018.0)[0] == "AGREE"


def test_classify_inci_formatting_agnostic():
    ours = "TALC · MICA · [+/-(MAY CONTAIN/PEUT CONTENIR): CI 77491]"
    theirs = "Talc, Mica, May Contain/Peut Contenir/(+/-): Ci 77491]\n"
    assert _classify_values("ingredients", ours, theirs) == ("AGREE", "")
    verdict = _classify_values("ingredients", ours, "Talc, Dimethicone, May Contain: Ci 77491")
    assert verdict[0] == "DISAGREE" and "base_diff" in verdict[1]


def test_brand_for_order():
    from bsb.config import brand_for_order, load_brands

    brands = load_brands()
    assert brand_for_order("OR26BZQN0001", brands) == "nars"
    assert brand_for_order("OR26BZOX0001", brands) == "olaplex"
    assert brand_for_order("OR26BZCSC0003", brands) == "colorescience"
    assert brand_for_order("OR26BZDRM0001", brands) == "aderma"
    assert brand_for_order("OR26RLGC0008", brands) is None  # not a BZ order
    assert brand_for_order(None, brands) is None
    assert brand_for_order("OR26BZZZ0001", brands) is None  # unknown code


def test_portal_errors_collect_and_draft(tmp_path, synonyms):
    from openpyxl import Workbook

    from bsb.portal import collect_portal_errors, draft_overrides

    wb = Workbook()
    ws = wb.active
    ws.append(["EAN Code", "Brand", "Boozt Errors"])
    ws.append(["194251140407", "NARS", "Invalid color code for category"])
    ws.append(["194251140414", "NARS", None])
    ws.append(["194251140421", "NARS", "Size format not accepted (4,8gr)"])
    path = tmp_path / "returned.xlsx"
    wb.save(path)

    errors = collect_portal_errors(str(path), synonyms)
    assert [e.ean for e in errors] == ["194251140407", "194251140421"]
    assert errors[0].field_guess == "color_code"
    assert errors[1].field_guess == "size"

    draft = draft_overrides("OR26BZQN0001", errors, tmp_path)
    text = draft.read_text()
    assert "portal rejection: Invalid color code" in text
    assert 'eans: ["194251140421"]' in text
    assert "value: FIXME" in text  # never auto-applied
