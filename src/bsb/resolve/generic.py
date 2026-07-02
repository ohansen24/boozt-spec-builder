"""Generic resolver: web search + JSON-LD GTIN matcher (Phase 1).

GTIN-anchored acceptance only: a source may be used for an EAN only if that
exact GTIN appears in the source's structured data or visible page content.
"""


def resolve(gtin13: str, brand: str) -> list:
    raise NotImplementedError("Phase 1: generic resolver not implemented yet")
