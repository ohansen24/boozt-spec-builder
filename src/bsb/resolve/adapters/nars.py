"""NARS SFCC adapter (Phase 1).

Strategy per build kit section 6.3: SFCC shop API with storefront client_id,
else Playwright render of the gtin13 PDP, else Firecrawl. Shade-family
efficiency: resolve 27 masters once, then confirm the 119 variants.
"""


def resolve(gtin13: str) -> dict:
    raise NotImplementedError("Phase 1: NARS adapter not implemented yet")
