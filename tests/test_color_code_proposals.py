"""Color-code auto-proposers (Oli 2026-07-06), proposals only — never
blocking, never overriding rules/confirmed lexicon, never writing a lexicon.

Stage 1: global colour-WORD map -> one anchor under the one-anchor rule.
"""

from bsb.categorize.rules import (
    color_code_for,
    propose_color_code_from_words,
)
from bsb.config import DEFAULT_CONFIG_DIR, load_rules

WM = load_rules(DEFAULT_CONFIG_DIR).get("color_word_map")


def test_word_map_loaded():
    assert WM and WM.get("words", {}).get("pink") == 1003


def test_single_anchor_proposes():
    for shade, code in [
        ("Baby-Pink", 1003), ("Intense Black", 1012), ("Wild Plum", 1008),
        ("2.75 Warm Auburn", 1010), ("Dark Cherry", 1009), ("1 Skinny Dip (Soft Peach)", 1021),
        ("3.5 Neutral Medium Brown", 1010),
    ]:
        d = propose_color_code_from_words(shade, WM)
        assert d.code == code and d.proposal, shade


def test_two_anchors_no_proposal():
    # ambiguity => no proposal, stays red
    for shade in ["Golden Brick Red", "Nude Pink", "5 Warm Black-Brown",
                  "10 French Toast (Mauve Brown)", "21 Summer Fling (Mauve Rose)"]:
        assert propose_color_code_from_words(shade, WM).code is None, shade


def test_opaque_and_modifier_only_no_proposal():
    for shade in ["Hoola", "Original", "Luna (Light Medium)", "Comet (Deep Dark)", "Natural"]:
        assert propose_color_code_from_words(shade, WM).code is None, shade


def test_same_anchor_repeated_still_proposes():
    # two colour words, ONE anchor -> propose (pomegranate + red both 1009)
    assert propose_color_code_from_words("Pomegranate Red", WM).code == 1009
    assert propose_color_code_from_words("Red Velvet (Ruby Red)", WM).code == 1009


def test_never_proposes_meta_codes():
    wm = {"words": {"clear": 1017, "multi": 1016, "natural": 1018, "pink": 1003}}
    assert propose_color_code_from_words("Clear", wm).code is None
    assert propose_color_code_from_words("Multi", wm).code is None
    assert propose_color_code_from_words("Pink", wm).code == 1003


def test_proposal_does_not_override_rules_or_lexicon(brands, rules):
    # foundation rule wins over any word in the shade (never a proposal)
    d = color_code_for("Foundation", "Pink Sand", rules, brands["benefit"], "Boi-ing Cakeless")
    assert d.code == 1018 and not d.proposal
    # a confirmed NARS lexicon hit wins, not a proposal
    d = color_code_for("Makeup", "Orgasm", rules, brands["nars"], "Blush")
    assert d.code == 1003 and d.rule.startswith("lexicon:") and not d.proposal


def test_path3_miss_now_proposes_instead_of_failing_closed(brands, rules):
    # Benefit blush shade, no lexicon -> was fail-closed, now a yellow proposal
    d = color_code_for("Makeup", "Wild Plum", rules, brands["benefit"], "BADgal BANG!")
    assert d.code == 1008 and d.proposal
    # but a genuinely ambiguous one still fails closed
    assert color_code_for("Makeup", "Nude Pink", rules, brands["benefit"], "x").code is None


def test_empty_or_none_shade_no_proposal():
    assert propose_color_code_from_words("", WM).code is None
    assert propose_color_code_from_words(None, WM).code is None
    assert propose_color_code_from_words("Rebel Brown", None).code is None  # no map -> no crash


# ---- Stage 2: swatch-hex -> anchor (perceptual ΔE) + two-signal ----------

from bsb.categorize.rules import (  # noqa: E402
    combine_color_proposals,
    propose_color_code_from_hex,
)

AH = WM.get("anchors_hex")


def test_hex_maps_to_nearest_anchor():
    for hx, code in [("1A1A1A", 1012), ("E0242B", 1009), ("D4AF37", 1015),
                     ("FFC0CB", 1003), ("8E4585", 1008)]:
        d = propose_color_code_from_hex(hx, AH)
        assert d.code == code and d.proposal, hx


def test_hex_never_proposes_meta_codes():
    # anchors_hex excludes 1016/1017/1018 by construction
    for code in (1016, 1017, 1018):
        assert code not in AH


def test_hex_malformed_no_proposal():
    for bad in [None, "", "ZZZ", "12345", "#12"]:
        assert propose_color_code_from_hex(bad, AH).code is None


def test_two_signals_agree_strengthens():
    w = propose_color_code_from_words("Wild Plum", WM)      # 1008
    h = propose_color_code_from_hex("8E4585", AH)           # purple -> 1008
    c = combine_color_proposals(w, h)
    assert c.code == 1008 and c.proposal and "two_signals_agree" in c.rule


def test_two_signals_disagree_withholds():
    w = propose_color_code_from_words("Wild Plum", WM)      # 1008 purple
    h = propose_color_code_from_hex("8C3A3A", AH)           # dark red-brown
    c = combine_color_proposals(w, h)
    assert c.code is None and c.rule.startswith("signals_disagree")


def test_hex_only_proposes_lower_confidence():
    # opaque name (no colour word) + a hex -> hex-only proposal
    w = propose_color_code_from_words("Hoola", WM)
    h = propose_color_code_from_hex("8B5A2B", AH)           # brown
    c = combine_color_proposals(w, h)
    assert w.code is None and c.code == h.code and "swatch_hex" in c.rule
