"""Schema-bound Anthropic fallback extractor (Phase 1).

Contract per build kit section 6.4: temperature 0, strict schema, verbatim
evidence snippet per field verified as a substring of the fetched source.
The LLM never sees ODM hints during extraction.
"""


def extract(fetched_text: str, gtin13: str) -> dict:
    raise NotImplementedError("Phase 1: LLM extractor not implemented yet")
