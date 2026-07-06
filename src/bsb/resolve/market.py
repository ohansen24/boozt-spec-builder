"""Retailer market classification (Oli refinement 2026-07-06).

GTIN anchoring is rightly country-agnostic — a valid GTIN match on any market's
page identifies the product. But INCI is a *regulatory* field: Boozt requires
EU-registered ingredient lists, and a US (or other non-EU) list may omit
allergen declarations the EU mandates. So each anchored retailer family is
tagged with its market, and the ingredients logic prefers EU/UK sources and
never greens on non-EU agreement alone.

Heuristic, by design (TLD + shop-locale + a small known-retailer map). "EEA"
(EU + Norway/Iceland/Liechtenstein) follows the EU Cosmetics Regulation, so it
counts as EU for INCI; the UK inherited it and is grouped with EU per Oli.
Anything we cannot confidently place as EU/UK is treated as non-EU (fail safe:
its INCI ships yellow with an allergen caveat, never green).
"""

from urllib.parse import urlsplit

# EEA ccTLDs (EU + Norway/Iceland/Liechtenstein) — EU Cosmetics Regulation applies
_EEA_CCTLD = {
    "de", "fr", "es", "it", "nl", "pl", "se", "dk", "fi", "at", "ie", "pt",
    "cz", "gr", "hu", "ro", "sk", "si", "hr", "lt", "lv", "ee", "lu", "bg",
    "cy", "mt", "be", "no", "is", "li", "eu",
}
_UK_CCTLD = {"uk"}
_US_CCTLD = {"us"}

# ambiguous gTLD hosts we have placed by hand (registrable first-label). Extend
# as new retailer families surface; unknown gTLD hosts fall through to OTHER.
_KNOWN_HOST_MARKET = {
    "lookfantastic": "UK",
    "cosmeterie": "EU",       # FR
    "haarshop": "EU",         # DE
    "bellaffair": "EU",       # DE
    "salontotal": "EU",       # DE
    "salonservicespro": "US",
    "premierbeautysupply": "US",
    "bluemercury": "US",
    "jomashop": "US",
}

# path/subdomain locale markers on gTLD hosts
_PATH_MARKET = [
    (("/en-gb", "/en_gb", "/gb/", "/uk/", "/en-uk"), "UK"),
    (("/en-us", "/en_us", "/us/", "/usa", "/en-usa"), "US"),
    (
        (
            "/de-", "/fr-", "/es-", "/it-", "/nl-", "/se-", "/dk-", "/fi-",
            "/en-eu", "/eu/", "/de/", "/fr/", "/es/", "/it/", "/nl/", "/se/",
        ),
        "EU",
    ),
]


def classify_market(url: str) -> str:
    """Return one of "EU" | "UK" | "US" | "OTHER" for a retailer URL."""
    split = urlsplit(url if "//" in url else f"//{url}")
    host = (split.netloc or split.path).lower().removeprefix("www.")
    path = (split.path or "").lower()

    # co.uk and other second-level ccTLDs first
    if host.endswith(".co.uk") or host.endswith(".uk"):
        return "UK"

    labels = host.split(".")
    tld = labels[-1] if labels else ""
    first = labels[0] if labels else host

    if first in _KNOWN_HOST_MARKET:
        return _KNOWN_HOST_MARKET[first]

    if tld in _UK_CCTLD:
        return "UK"
    if tld in _US_CCTLD:
        return "US"
    if tld in _EEA_CCTLD:
        return "EU"

    # gTLD (com/net/org/co/shop/store/…): lean on path/subdomain locale
    for markers, market in _PATH_MARKET:
        if any(m in path for m in markers):
            return market
    for markers, market in _PATH_MARKET:
        if any(m.strip("/-") and host.startswith(m.strip("/-") + ".") for m in markers):
            return market

    return "OTHER"


def is_eu_market(market: str | None) -> bool:
    """EU/UK sources satisfy Boozt's EU-registered-INCI requirement."""
    return market in ("EU", "UK")
