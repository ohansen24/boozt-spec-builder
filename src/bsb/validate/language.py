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
# STRONG signals: words that are essentially never in an English product name —
# foreign cosmetic/hair nouns. One is enough to flag (source page lang differs
# from the name text, so text is decisive).
_STRONG_NON_EN = {
    "włosów", "odżywka", "szampon", "pielęgnacja",              # pl
    "tratament", "păr", "par", "îngrijire", "sampon",           # ro
    "cheveux", "traitement", "soin", "aprés-shampooing",        # fr
    "cabello", "champú", "acondicionador", "tratamiento", "capilar",  # es
    "capelli", "trattamento", "balsamo",                        # it
    "cabelo", "tratamento",                                     # pt
    "haare", "haar", "pflege", "spülung", "haarshampoo",        # de/nl
    "hår", "hoito",                                             # scandinavian/fi
}
# WEAK signals: function words that also appear (rarely) in English; need two.
_WEAK_NON_EN = {
    "do", "dla", "i", "w", "z", "na", "und", "für", "mit", "ohne", "pour",
    "avec", "sans", "et", "les", "des", "och", "för", "med", "til", "per",
    "con", "sin", "voor", "met", "para", "pentru",
}
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


# a color_name still carrying a leading "N - "/"N-" shade-code separator has
# not been through a per-brand shade_format — surface it for a human decision
# (the number may BE the shade identity, e.g. Benefit; never silently stripped)
_LEADING_NUM_SEP = re.compile(r"^\s*\d+(?:\.\d+)?\s*[-–—]\s")  # noqa: RUF001


def leading_numeric_separator(text: str | None) -> bool:
    return bool(text) and bool(_LEADING_NUM_SEP.match(text))


def has_non_latin(text: str | None) -> bool:
    return bool(text) and bool(_NON_LATIN.search(text))


def non_english_tokens(text: str | None) -> list[str]:
    """Signals that a NAME is not English: non-Latin script (definite), an
    unambiguous foreign cosmetic word (strong, one suffices), or two+ localized
    function words (weak, robust to an English homograph like "per"). Empty
    list => reads English. Deliberately text-only: an English name on a foreign
    storefront (e.g. "Purifying Cleanse Shampoo" on a .pl page) must still
    ship."""
    if not text:
        return []
    hits: list[str] = []
    if _NON_LATIN.search(text):
        hits.append("non-Latin-script")
    words = [w for w in _WORD.findall(text.casefold())]
    hits += [w for w in words if w in _STRONG_NON_EN]
    weak = [w for w in words if w in _WEAK_NON_EN]
    if len(weak) >= 2:
        hits += weak
    return hits


def is_english_name(text: str | None, source_language: str | None = None) -> bool:
    """A name is shippable only if the NAME TEXT reads as English (non-Latin
    script or localized function words fail it). The source page's language is
    deliberately NOT decisive — a non-English storefront often lists the
    English product name ("Purifying Cleanse Shampoo" on a .pl page), and Boozt
    wants that English name. ``source_language`` is accepted for callers that
    record it in a note, but never rejects an English-reading name."""
    if not text:
        return True  # emptiness is handled by the caller's fail-closed path
    return not non_english_tokens(text)
