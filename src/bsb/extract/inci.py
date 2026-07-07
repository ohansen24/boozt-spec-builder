"""Retailer INCI extraction (Phase 2 capability gap #1).

Runs ONLY on GTIN-anchored retailer PDPs. Deterministic parse first:
labeled sections ("Ingredients", "INCI", "Zusammensetzung", "Composition",
"Ingrédients", "Inhaltsstoffe"), structured description blocks, accordion
payloads. Every candidate passes the plausibility lint before acceptance —
nomenclature tokens, a plausible leading ingredient, no marketing prose, no
mid-list truncation. Where only the schema-bound LLM extractor could isolate
a block (key-gated, evidence-substring rule), the deterministic path returns
None and the caller may escalate; the stored value is always verbatim page
content.
"""

import re

from bs4 import BeautifulSoup
from pydantic import BaseModel

_LABEL = re.compile(
    r"^\s*(?:full\s+)?(ingredients?|inci|zusammensetzung|composition|ingr[ée]dients?"
    r"|inhaltsstoffe|bestandteile|liste\s+des\s+ingr[ée]dients)\s*:?\s*$",
    re.IGNORECASE,
)
_LABEL_INLINE = re.compile(
    r"(?:full\s+)?(?:ingredients?|inci|zusammensetzung|composition|ingr[ée]dients?"
    r"|inhaltsstoffe|bestandteile)\s*:\s*",
    re.IGNORECASE,
)
_SPLIT = re.compile(r"\s*[,·•;]\s*")
_TAGS = re.compile(r"<[^>]+>")

# plausible leading ingredients (most cosmetic formulas open with one of these
# classes; power sources for the "is this really an INCI list" test). Includes
# aerosol propellants — dry shampoos, hairsprays, mousses and spray deodorants
# legitimately lead with butane/propane/hydrofluorocarbon, not Aqua (seen live:
# Maria Nila dry shampoos rejected valid propellant-led INCI).
_LEAD_TOKENS = re.compile(
    r"^(aqua|water|eau|alcohol|glycerin|glycerine|dimethicone|isododecane|talc"
    r"|paraffinum|petrolatum|butyrospermum|caprylic|hydrogenated|squalane"
    r"|cyclopentasiloxane|mineral oil|sodium|zinc oxide|titanium dioxide"
    r"|butane|isobutane|propane|isopentane|pentane|dimethyl ether"
    r"|hydrofluorocarbon|hydrofluoroolefin|propylene glycol|propanediol"
    r"|cocamidopropyl|coco-glucoside|decyl glucoside|cyclohexasiloxane"
    r"|c\d)\b",
    re.IGNORECASE,
)
_MARKETING = re.compile(
    r"\b(helps?|your|skin feels|apply|use daily|discover|formulated to|leaves"
    r"|enriched|delivers|hilft|ihre haut|appliquer|peaux?)\b",
    re.IGNORECASE,
)
# retailer page furniture that leaks into a scraped "ingredient list": a
# concentration table ("52 0 sodium-lauroyl-sarcosinate 24 0 …"), a list header
# ("26 ingredients", "ingredient information") or a truncated link ("see full
# ingredients list"). None of these are ingredient names.
# \d{1,3}\s+\d{1,3}: the small "52 0 … 24 0" pairs of a concentration column —
# bounded to short ints so it does NOT collide with space-joined 5-digit Colour
# Index codes ("CI 77491 77492 77499"), which are a legitimate colorant grouping.
_INCI_GARBAGE = re.compile(
    r"\b\d{1,3}\s+\d{1,3}\b|\bingredient information\b|\bsee full\b"
    r"|\bingredients?\s+list\b|^\s*\d+\s+ingredients?\b",
    re.IGNORECASE,
)
# legal/disclaimer copy some brand accordions weave into the INGREDIENTS block
# ("… [+/- Ci 77891]. Disclaimer: Product ingredient listings are updated
# periodically. Before using … please read the ingredient list on the
# packaging …"). Seen live: Benefit SFCC accordions, OR26BZFT0001.
_INCI_TRAILER = re.compile(
    r"\bdisclaimer\b|\bproduct ingredient listings?\b|\bplease read the ingredient",
    re.IGNORECASE,
)
# prose function/legal words that never occur inside an INCI token but pepper a
# disclaimer sentence. Used to drop prose SEGMENTS while keeping real
# ingredients (even all-lowercase multi-word ones like "ammonium acrylates
# copolymer", which a lowercase-word count would wrongly flag).
_INCI_PROSE = re.compile(
    r"\b(?:the|of|to|for|your|you|are|please|before|after|using|read|updated"
    r"|periodically|recommend|personal|between|check|suppliers|disclaimer"
    r"|listings?|information|our|their|appropriate|packaging|product)\b",
    re.IGNORECASE,
)
# segment boundaries: INCI separators plus a sentence break (". ") so a disclaimer
# fused onto the list ("…(titanium dioxide)]. Disclaimer:…", or a leading
# "Product ingredient listings… Aqua ·") is isolated from real ingredients.
_INCI_SEG_SPLIT = re.compile(r"[,·•;]|\.\s+")


def _is_inci_prose(segment: str) -> bool:
    return bool(_INCI_TRAILER.search(segment) or _MARKETING.search(segment)
                or _INCI_PROSE.search(segment))


def strip_inci_trailer(text: str) -> str:
    """Remove legal/disclaimer PROSE (a leading preamble, a mid-list note, or a
    trailing paragraph) from an extracted INCI, preserving the ingredient tokens
    and the may-contain block around it. Content-preserving and
    position-agnostic — a blunt cut-to-end deleted leading/mid-list content
    (INCI review 2026-07). Only activates when a legal marker is present, so a
    clean list is returned untouched."""
    if not _INCI_TRAILER.search(text):
        return text
    kept = [
        seg.strip()
        for seg in _INCI_SEG_SPLIT.split(text)
        if seg.strip() and not _is_inci_prose(seg)
    ]
    return ", ".join(kept)


class InciCandidate(BaseModel):
    text: str  # verbatim page content (never rewritten)
    source: str  # labeled-section | inline-label | description-block
    tokens: int


def inci_plausible(text: str, labeled: bool = False) -> tuple[bool, str]:
    """Nomenclature-shaped, plausible lead, no prose, not truncated.

    ``labeled`` = the block was found under an explicit "Ingredients:"/"INCI:"
    label. That label is authoritative that this IS an ingredient list, so the
    narrow leading-ingredient whitelist (a proxy used to GUESS unlabeled
    blocks) is skipped — otherwise valid lists that open with an ingredient not
    in the whitelist (aerosol propellants, Cyclomethicone-led oils, starch-led
    powders) are wrongly rejected. The structural guards (token count, no
    marketing prose, no mid-list truncation) still apply either way."""
    cleaned = " ".join(text.split())
    if len(cleaned) < 40:
        return False, "too short"
    tokens = [t for t in _SPLIT.split(cleaned) if t.strip()]
    if len(tokens) < 5:
        return False, f"only {len(tokens)} separator-delimited tokens"
    # redacted list: a standalone all-asterisk token ("*******") is a hidden
    # ingredient (incibeauty and similar databases mask/paywall them). A masked
    # list is incomplete and must never ship — a trailing organic marker
    # ("Aqua*") is fine, but a token that is ONLY asterisks is a redaction.
    if any(re.fullmatch(r"\*+", t.strip()) for t in tokens):
        return False, "contains a masked/redacted ingredient (asterisks)"
    # a single INCI token is a short nomenclature name; an over-long comma
    # segment is leaked page copy (a disclaimer, marketing sentence or a "see
    # full ingredients list" link), never an ingredient — reject regardless of
    # how long the whole list is, so one or two prose runs can't slip under the
    # fractional prose gate below (seen live on the generic-validator path:
    # salontotal disclaimer, bluemercury marketing, world.* concentration dump).
    # Slash separators are not word boundaries here — a spaced multilingual name
    # ("Aqua / Water / Eau / Wasser / …") is one ingredient, not prose.
    if any(sum(1 for w in t.split() if w != "/") > 12 for t in tokens):
        return False, "over-long token (page copy leaked into the list)"
    if any(_INCI_GARBAGE.search(t) for t in tokens):
        return False, "concentration table / list header leaked into the list"
    if not labeled and not _LEAD_TOKENS.match(tokens[0].strip("[](): ")):
        return False, f"implausible leading ingredient {tokens[0][:30]!r}"
    prose = [t for t in tokens if _MARKETING.search(t) or len(t.split()) > 7]
    if len(prose) > max(1, len(tokens) // 10):
        return False, f"marketing prose in {len(prose)} tokens"
    if cleaned.endswith((",", "·", ";", "…", "...")):
        return False, "truncated mid-list"
    return True, ""


_SENTENCE_BREAK = re.compile(r"[.!?]\s+[A-ZÅÄÖÜÉÈ]")


def _lowercase_words(token: str) -> int:
    """Count all-lowercase alphabetic words (len>=3) — a proxy for prose.
    INCI tokens are Title-Case/ALL-CAPS nomenclature (possibly with slashes and
    parentheticals), so they carry ~0 lowercase words even when long
    ("Candelilla Cera/Euphorbia Cerifera (Candelilla) Wax/Cire De Candelilla");
    a sentence fragment carries several ("spraya på torrt hår för")."""
    return sum(1 for w in token.split() if w.isalpha() and w.islower() and len(w) >= 3)


def _leading_inci_run(text: str) -> str:
    """Keep only the leading comma-delimited run of INCI-shaped tokens, cutting
    at the first token that reads as prose (a marketing word, a sentence break,
    or 3+ lowercase connective words). Language-robust: bounds an inline-label
    grab to the actual list without enumerating "How to use"/"Användning"/…
    stop-headings for every storefront locale (seen live: Maria Nila .se pages
    ran the grab past the list into Swedish copy). The lowercase-word test —
    not a raw word count — preserves long multilingual INCI tokens."""
    kept: list[str] = []
    for part in _SPLIT.split(text):
        token = part.strip()
        if not token:
            continue
        prose = (
            _MARKETING.search(token)
            or _SENTENCE_BREAK.search(token)
            or _lowercase_words(token) >= 3
        )
        if prose:
            break
        kept.append(token)
    return ", ".join(kept)


def _text_of(element) -> str:
    return " ".join(element.get_text(" ", strip=True).split())


def extract_inci_from_html(html: str) -> InciCandidate | None:
    """Best plausible INCI block from a retailer PDP, deterministic only."""
    soup = BeautifulSoup(html, "lxml")
    candidates: list[InciCandidate] = []

    # 1) labeled sections: a short label element, list follows in the next
    #    sibling(s) or the parent's remaining text
    for element in soup.find_all(
        ["h1", "h2", "h3", "h4", "h5", "strong", "b", "dt", "span", "p", "button", "a"]
    ):
        label_text = _text_of(element)
        if not label_text or len(label_text) > 45 or not _LABEL.match(label_text):
            continue
        for sibling in list(element.find_next_siblings())[:3]:
            text = _text_of(sibling)
            ok, _ = inci_plausible(text, labeled=True)
            if ok:
                candidates.append(
                    InciCandidate(text=text, source="labeled-section", tokens=text.count(",") + 1)
                )
                break
        else:
            parent = element.parent
            if parent is not None:
                text = _text_of(parent)
                text = _LABEL_INLINE.sub("", text, count=1)
                # drop the bare label if it leads the text
                text = re.sub(_LABEL, "", text).strip()
                ok, _ = inci_plausible(text, labeled=True)
                if ok:
                    candidates.append(
                        InciCandidate(
                            text=text, source="labeled-section", tokens=text.count(",") + 1
                        )
                    )

    # 2) inline labels anywhere in flattened text ("Ingredients: Aqua, …")
    flat = _TAGS.sub(" ", html)
    flat = " ".join(flat.split())
    for match in _LABEL_INLINE.finditer(flat):
        segment = flat[match.end() : match.end() + 4000]
        # stop at the next obvious section heading …
        stop = re.search(
            r"(?:How to use|Anwendung|Anwendungshinweise|Hinweis|Conseils|Avis|Reviews"
            r"|Warnings|Utilisation|Précautions|La liste des ingrédients"
            r"|The ingredient lists?)\b",
            segment,
            re.IGNORECASE,
        )
        if stop:
            segment = segment[: stop.start()]
        # … and at inline CSS/JS that follows the list (Shopify inlines a
        # <style> block right after the ingredients <p>; tag-stripping runs the
        # CSS into the list and the plausibility lint reads it as prose)
        css = re.search(
            r"(?:@media|@font-face|@keyframes|/\*|<style|\bfunction\s*\(|[{}])", segment
        )
        if css:
            segment = segment[: css.start()]
        # a page that renders the list twice (mobile + desktop DOM) leaves a
        # second "Ingredients:" label inside the window — cut at it so the value
        # is the single list, not a doubled one
        dup = _LABEL_INLINE.search(segment)
        if dup:
            segment = segment[: dup.start()]
        # trim to the leading INCI run (locale-agnostic list boundary)
        segment = _leading_inci_run(segment).strip().rstrip(",;· ")
        ok, _ = inci_plausible(segment, labeled=True)
        if ok:
            candidates.append(
                InciCandidate(text=segment, source="inline-label", tokens=segment.count(",") + 1)
            )

    # 3) structured description blocks (itemprop/description accordions)
    selector = (
        '[itemprop="description"], [class*="ingredient"], '
        '[id*="ingredient"], [class*="composition"]'
    )
    for element in soup.select(selector):
        text = _text_of(element)
        text = _LABEL_INLINE.sub("", text, count=1)
        ok, _ = inci_plausible(text)
        if ok:
            candidates.append(
                InciCandidate(text=text, source="description-block", tokens=text.count(",") + 1)
            )

    if not candidates:
        return None
    # most token-dense candidate wins; prefer labeled sections on ties
    order = {"labeled-section": 0, "inline-label": 1, "description-block": 2}
    candidates.sort(key=lambda c: (-c.tokens, order[c.source], len(c.text)))
    return candidates[0]
