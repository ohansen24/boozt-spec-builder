"""English-name policy (Oli 2026-07-06). Boozt requires English style_name /
color_name. A non-English name is never shippable as a value — never
translated: when only non-English sources exist the cell fails closed (empty +
red) with a note. The generic resolver's shipped retail names are the only
place non-English text enters (brand sites and lookfantastic are English).
"""

import re

# definitively non-English scripts (Cyrillic, Greek, CJK, Hebrew, Arabic, Thai)
_NON_LATIN = re.compile(
    r"[Ѐ-ӿͰ-Ͽ一-鿿぀-ヿ가-힯"
    r"֐-׿؀-ۿ฀-๿]"
)
# source-page languages that are not English (tld/lang-attr heuristic values)
_NON_EN_LANGS = {
    "pl", "de", "fr", "es", "it", "nl", "sv", "da", "fi", "pt", "cs", "sk",
    "ru", "uk", "no", "hu", "ro", "bg", "hr", "sl", "lt", "lv", "et", "el", "tr",
}
# non-English function words that betray a localized name even in Latin script
_NON_EN_WORDS = {
    "do", "dla", "włosów", "i", "w", "z", "na", "und", "für", "mit", "ohne",
    "pour", "avec", "sans", "et", "les", "des", "och", "för", "med", "til",
    "per", "con", "sin", "voor", "met", "haar", "haare", "cheveux", "cabello",
}
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def has_non_latin(text: str | None) -> bool:
    return bool(text) and bool(_NON_LATIN.search(text))


def non_english_tokens(text: str | None) -> list[str]:
    """Signals that a name is not English: non-Latin script, or localized
    function words. Empty list => looks English (ASCII/Latin, no giveaways)."""
    if not text:
        return []
    hits: list[str] = []
    if _NON_LATIN.search(text):
        hits.append("non-Latin-script")
    for word in _WORD.findall(text.casefold()):
        if word in _NON_EN_WORDS:
            hits.append(word)
    return hits


def is_english_name(text: str | None, source_language: str | None = None) -> bool:
    """A name is shippable only if it reads as English. Uses the text itself
    (script + function words) and the source page's language when known."""
    if not text:
        return True  # emptiness is handled by the caller's fail-closed path
    if non_english_tokens(text):
        return False
    return not (source_language and source_language.casefold() in _NON_EN_LANGS)
