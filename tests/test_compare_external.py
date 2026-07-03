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
