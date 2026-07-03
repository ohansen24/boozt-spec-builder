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
# classes; power sources for the "is this really an INCI list" test)
_LEAD_TOKENS = re.compile(
    r"^(aqua|water|eau|alcohol|glycerin|glycerine|dimethicone|isododecane|talc"
    r"|paraffinum|petrolatum|butyrospermum|caprylic|hydrogenated|squalane"
    r"|cyclopentasiloxane|mineral oil|sodium|zinc oxide|titanium dioxide)\b",
    re.IGNORECASE,
)
_MARKETING = re.compile(
    r"\b(helps?|your|skin feels|apply|use daily|discover|formulated to|leaves"
    r"|enriched|delivers|hilft|ihre haut|appliquer|peaux?)\b",
    re.IGNORECASE,
)


class InciCandidate(BaseModel):
    text: str  # verbatim page content (never rewritten)
    source: str  # labeled-section | inline-label | description-block
    tokens: int


def inci_plausible(text: str) -> tuple[bool, str]:
    """Nomenclature-shaped, plausible lead, no prose, not truncated."""
    cleaned = " ".join(text.split())
    if len(cleaned) < 40:
        return False, "too short"
    tokens = [t for t in _SPLIT.split(cleaned) if t.strip()]
    if len(tokens) < 5:
        return False, f"only {len(tokens)} separator-delimited tokens"
    if not _LEAD_TOKENS.match(tokens[0].strip("[](): ")):
        return False, f"implausible leading ingredient {tokens[0][:30]!r}"
    prose = [t for t in tokens if _MARKETING.search(t) or len(t.split()) > 7]
    if len(prose) > max(1, len(tokens) // 10):
        return False, f"marketing prose in {len(prose)} tokens"
    if cleaned.endswith((",", "·", ";", "…", "...")):
        return False, "truncated mid-list"
    return True, ""


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
            ok, _ = inci_plausible(text)
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
                ok, _ = inci_plausible(text)
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
        # stop at the next obvious section heading
        stop = re.search(
            r"(?:How to use|Anwendung|Anwendungshinweise|Hinweis|Conseils|Avis|Reviews"
            r"|Warnings|Utilisation|Précautions|La liste des ingrédients"
            r"|The ingredient lists?)\b",
            segment,
            re.IGNORECASE,
        )
        if stop:
            segment = segment[: stop.start()]
        segment = segment.strip()
        ok, _ = inci_plausible(segment)
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
