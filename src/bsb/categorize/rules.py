"""Product Category and Boozt Color code decision rules (build kit 6.7).

Keyed on the product name (and curated brand knowledge), never the ODM
subcategory. Fail closed: anything the rules cannot decide returns None and
stays empty and red — the tool never invents a category.

The foundation-family rule runs first: foundations, concealers, BB/CC creams
and tinted moisturizers go to "Foundation", not "Makeup". That is the single
biggest error surface in OR26BZQN0001 (~67 of 119 rows the ODM calls
"Face Make-Up").
"""

import re

from pydantic import BaseModel


class CategoryDecision(BaseModel):
    category: str | None = None
    rule: str | None = None  # e.g. 'foundation_family:concealer', 'brand:multiple'


class ColorCodeDecision(BaseModel):
    code: int | None = None
    rule: str | None = None
    pending_confirmation: bool = False  # open question 2 -> emit yellow
    proposal: bool = False  # Stage 1/2 auto-proposal -> yellow, "please confirm"
    proposal_note: str | None = None  # human-facing rationale for the proposal
    low_chroma: bool = False  # (hex proposals) swatch is a low-chroma neutral -> weak


# meta-codes the auto-proposers must never emit (only rules assign these)
_NEVER_PROPOSE = {1016, 1017, 1018}
# low-chroma neutral anchors: a chromatic word must not be vetoed by a hex that
# lands here (grey/beige/cream/silver). Black/White stay strong (mascara etc.).
_NEUTRAL_ANCHORS = {1001, 1002, 1011, 1014}
_DEFAULT_LOW_CHROMA = 20.0
_WORD_TOKEN = re.compile(r"[^\W\d_]+", re.UNICODE)
_INCI_TOKEN_SPLIT = re.compile(r"[,·•;]")


def flammable_from_inci(inci_text: str | None, rules: dict) -> tuple[str | None, list[str]]:
    """Decide flammability from the ingredient list — an aerosol propellant or
    volatile solvent (config: flammable_inci_markers) makes a product flammable,
    regardless of its Boozt category. Returns ("Yes", [matched markers]) if any
    marker is present, ("No", []) if the INCI is present with none, or
    (None, []) when there is no INCI to judge (caller falls back to the category
    heuristic). Tokens are compared despaced/dehyphenated so localised spellings
    ("Dimethylether", "alcoholdenat.") still match; parentheticals are dropped."""
    if not inci_text or not str(inci_text).strip():
        return None, []
    markers = rules.get("flammable_inci_markers") or {}
    exact = {str(m).lower() for m in markers.get("exact", [])}
    prefixes = tuple(str(m).lower() for m in markers.get("prefix", []))
    hits: list[str] = []
    for raw in _INCI_TOKEN_SPLIT.split(str(inci_text)):
        token = re.sub(r"\(.*?\)", "", raw).strip().lower().strip(" .")
        key = re.sub(r"[\s\-]", "", token)  # despace + dehyphen
        if key in exact or (prefixes and key.startswith(prefixes)):
            hits.append(token)
    return ("Yes", hits) if hits else ("No", [])


def _hex_to_lab(value: str | None) -> tuple[float, float, float] | None:
    """sRGB hex -> CIELAB (D65), pure Python. None on a malformed hex."""
    if not value:
        return None
    h = value.strip().lstrip("#")
    if len(h) != 6 or any(c not in "0123456789abcdefABCDEF" for c in h):
        return None
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))

    def _lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = _lin(r), _lin(g), _lin(b)
    x = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.95047
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = (r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.08883

    def _f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else (7.787 * t + 16 / 116)

    fx, fy, fz = _f(x), _f(y), _f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _delta_e(a: tuple, b: tuple) -> float:
    return sum((p - q) ** 2 for p, q in zip(a, b, strict=False)) ** 0.5


def propose_color_code_from_hex(
    hex_value: str | None,
    anchors_hex: dict | None,
    low_chroma_threshold: float = _DEFAULT_LOW_CHROMA,
) -> ColorCodeDecision:
    """Stage 2: nearest CHROMATIC anchor to a swatch hex by CIELAB ΔE. Never the
    meta-codes 1016/1017/1018 (excluded from anchors_hex). A proposal; the
    caller decides confidence (agrees-with-word vs hex-only). Marks low_chroma
    when the swatch's own C* is below threshold (a weak, neutral signal)."""
    lab = _hex_to_lab(hex_value)
    if lab is None or not anchors_hex:
        return ColorCodeDecision()
    chroma = (lab[1] ** 2 + lab[2] ** 2) ** 0.5
    best_code, best_d = None, None
    for code, ahex in anchors_hex.items():
        if int(code) in _NEVER_PROPOSE:
            continue
        alab = _hex_to_lab(str(ahex))
        if alab is None:
            continue
        d = _delta_e(lab, alab)
        if best_d is None or d < best_d:
            best_code, best_d = int(code), d
    if best_code is None:
        return ColorCodeDecision()
    clean = "#" + hex_value.strip().lstrip("#").upper()
    return ColorCodeDecision(
        code=best_code,
        rule=f"swatch_hex:{clean}->{best_code}(dE{best_d:.0f})",
        proposal=True,
        proposal_note=f"proposed from swatch hex {clean} — lower confidence, please confirm",
        low_chroma=chroma < low_chroma_threshold,
    )


def combine_color_proposals(
    word: ColorCodeDecision, hexp: ColorCodeDecision
) -> ColorCodeDecision:
    """Two-signal rule (Oli): word+hex AGREE -> stronger proposal; DISAGREE ->
    no proposal, both signals shown (red for a human); word-only -> Stage 1
    proposal; hex-only -> lower-confidence proposal."""
    if word.code is not None and hexp.code is not None:
        if word.code == hexp.code:
            return ColorCodeDecision(
                code=word.code,
                rule=f"two_signals_agree:{word.rule}+{hexp.rule}",
                proposal=True,
                proposal_note="two signals agree (shade word + swatch hex) — please confirm",
            )
        # signal weighting: a low-chroma (neutral) swatch is a WEAK signal and
        # does not veto an unambiguous chromatic word -> word-only proposal
        if hexp.low_chroma and word.code not in _NEUTRAL_ANCHORS:
            return ColorCodeDecision(
                code=word.code,
                rule=f"word_over_low_chroma_hex:{word.rule} (hex {hexp.rule})",
                proposal=True,
                proposal_note="swatch hex low-chroma (weak) — word signal used; please confirm",
            )
        # strong-vs-strong disagreement: withhold, surface both for the human
        return ColorCodeDecision(
            code=None,
            rule=f"signals_disagree:word->{word.code} vs {hexp.rule}",
        )
    if word.code is not None:
        return word
    if hexp.code is not None:
        return hexp
    return ColorCodeDecision()


def propose_color_code_from_words(shade: str | None, word_map: dict | None) -> ColorCodeDecision:
    """Stage 1: read plain colour words out of a shade name and propose the
    matching anchor. Modifier/undertone words are ignored. Propose only when
    all matched colour words resolve to exactly ONE anchor (documented
    multi-word rule); zero or 2+ distinct anchors -> no proposal (fail closed).
    Never emits the meta-codes 1016/1017/1018."""
    words_cfg = (word_map or {}).get("words") or {}
    if not shade or not words_cfg:
        return ColorCodeDecision()
    matched: list[str] = []
    anchors: set[int] = set()
    for token in _WORD_TOKEN.findall(shade.casefold()):
        code = words_cfg.get(token)
        if code is not None and int(code) not in _NEVER_PROPOSE:
            anchors.add(int(code))
            matched.append(token)
    if len(anchors) != 1:
        return ColorCodeDecision()  # ambiguous or no colour word
    code = next(iter(anchors))
    return ColorCodeDecision(
        code=code,
        rule=f"color_word:{'+'.join(dict.fromkeys(matched))}->{code}",
        proposal=True,
        proposal_note="proposed from shade name — please confirm or correct",
    )


def _keyword_match(name: str, keyword: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", name) is not None


def categorize(
    product_name: str,
    rules: dict,
    brand_cfg: dict | None = None,
    site_category_id: str | None = None,
) -> CategoryDecision:
    """Decide the Boozt Product Category for a product name.

    Decision priority:
    1. The brand's own first-party site category (``site_category_id`` mapped
       through ``brand_cfg.site_category_map``). Storefronts whose product
       names are pure marketing names ("Shellie", "Benetint") carry no category
       keyword, so their GTIN-anchored datalayer categoryID is the most
       reliable signal — and it still fails closed for any id not in the map.
    2. Generic keyword rules (foundation_family first, per yaml order).
    3. Brand-curated product-name keywords.
    """
    site_map = (brand_cfg or {}).get("site_category_map") or {}
    if site_category_id and site_category_id in site_map:
        return CategoryDecision(
            category=site_map[site_category_id], rule=f"site_category:{site_category_id}"
        )

    name = product_name.casefold()

    for group_name, group in rules["category_rules"].items():
        for keyword in group["keywords"]:
            if _keyword_match(name, keyword):
                return CategoryDecision(category=group["category"], rule=f"{group_name}:{keyword}")

    for keyword, category in (brand_cfg or {}).get("product_name_categories", {}).items():
        if _keyword_match(name, keyword):
            return CategoryDecision(category=category, rule=f"brand:{keyword}")

    return CategoryDecision()  # fail closed


def is_multi_shade_product(product_name: str | None, rules: dict) -> bool:
    """Palettes, quads, trios: one shade name, several colors — the shade
    lexicon must never decide these."""
    if not product_name:
        return False
    name = product_name.casefold()
    markers = (rules.get("color_code_rules") or {}).get("multi_shade_markers") or []
    return any(re.search(rf"(?<!\w){re.escape(str(m).casefold())}(?!\w)", name) for m in markers)


def color_code_for(
    category: str | None,
    shade: str | None,
    rules: dict,
    brand_cfg: dict | None = None,
    product_name: str | None = None,
    swatch_hex: str | None = None,
) -> ColorCodeDecision:
    """Boozt Color code (1001-1022) per build kit 6.7: rule 1
    (skincare/colorless -> 1017), rule 2 (foundation family -> 1018) and
    rule 3b (curated shade lexicon, keyed per BRAND — shade names collide
    across brands). Multi-shade products bypass the lexicon and fail closed
    until a product-type rule exists. Swatch-hex and LLM proposals are
    Phase 1. None -> empty and red."""
    cc_rules = rules["color_code_rules"]

    if category == "Foundation":
        return ColorCodeDecision(
            code=cc_rules["foundation_family_code"],
            rule="foundation_family",
            pending_confirmation=bool(cc_rules.get("foundation_family_pending")),
        )

    if category in cc_rules["clear_categories"] and not (shade and shade.strip()):
        # colorless skincare/hair care -> 1017; a shade-bearing item in these
        # categories (Colour Refresh hair masks) must NOT be forced to Clear
        return ColorCodeDecision(code=1017, rule="clear_category")

    if is_multi_shade_product(product_name, rules):
        # never the lexicon: one shade name, several colors. With a confirmed
        # product-type rule the code ships; without one it fails closed.
        default = cc_rules.get("multi_shade_default")
        if default:
            return ColorCodeDecision(code=int(default), rule="multi_shade_default")
        return ColorCodeDecision(rule="multi_shade_product")

    if shade:
        needle = shade.casefold().strip()
        for entry in (brand_cfg or {}).get("shade_lexicon") or []:
            lexeme = str(entry["shade"]).casefold()
            hit = needle.startswith(lexeme) if entry.get("match") == "prefix" else needle == lexeme
            if hit:
                return ColorCodeDecision(code=int(entry["code"]), rule=f"lexicon:{lexeme}")

    # path-3 fallback: auto-PROPOSALS (never override the rules or a confirmed
    # lexicon hit above). Stage 1 = colour word; Stage 2 = swatch hex (ΔE);
    # combined per the two-signal rule. Always yellow / "please confirm".
    word_map = rules.get("color_word_map") or {}
    word = propose_color_code_from_words(shade, word_map)
    hexp = propose_color_code_from_hex(
        swatch_hex,
        word_map.get("anchors_hex"),
        float(word_map.get("low_chroma_threshold", _DEFAULT_LOW_CHROMA)),
    )
    combined = combine_color_proposals(word, hexp)
    disagree = combined.rule and combined.rule.startswith("signals_disagree")
    if combined.code is not None or disagree:
        return combined

    return ColorCodeDecision()  # fail closed
