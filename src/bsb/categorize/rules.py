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


# meta-codes the auto-proposers must never emit (only rules assign these)
_NEVER_PROPOSE = {1016, 1017, 1018}
_WORD_TOKEN = re.compile(r"[^\W\d_]+", re.UNICODE)


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

    # path-3 fallback: a colour-word PROPOSAL (never overrides the rules or a
    # confirmed lexicon hit above). Yellow, "please confirm" — Stage 1.
    proposal = propose_color_code_from_words(shade, rules.get("color_word_map"))
    if proposal.code is not None:
        return proposal

    return ColorCodeDecision()  # fail closed
