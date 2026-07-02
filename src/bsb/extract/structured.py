"""Structured extraction: SFCC product-state payloads and JSON-LD.

Extraction over generation (charter principle 3): everything here parses
structured payloads embedded in fetched HTML and records where it came from.

narscosmetics.eu (SFRA) embeds one product-state JSON object per page:
`var productCache = {...}` on full PDPs and `pdpdata = {...}` in
Product-Variation ajax partials — same schema either way, carrying the
SELECTED variant id ("ID"), "masterID", "name", the "variants" map keyed by
"color-{gtin13}", and a "variations" attribute list with shade values.
"""

import json
import re

_JSONLD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)
_STATE_ANCHORS = ("var productCache =", "pdpdata =")


def extract_json_object(text: str, anchor: str) -> dict | None:
    """Parse the JSON object that starts at the first '{' after `anchor`,
    using a string-aware brace scan (the object may contain braces inside
    string values)."""
    i = text.find(anchor)
    if i == -1:
        return None
    start = text.find("{", i + len(anchor))
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for pos in range(start, len(text)):
        ch = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : pos + 1])
                except json.JSONDecodeError:
                    return None
    return None


def parse_sfcc_product_state(html: str) -> dict | None:
    """The SFRA product-state object from a PDP or a variation partial."""
    for anchor in _STATE_ANCHORS:
        state = extract_json_object(html, anchor)
        if state is not None and "ID" in state:
            return state
    return None


def parse_jsonld_products(html: str) -> list[dict]:
    """All JSON-LD Product nodes, flattening @graph containers."""
    products = []
    for match in _JSONLD_RE.finditer(html):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else data.get("@graph", [data])
        for node in nodes:
            if isinstance(node, dict) and node.get("@type") == "Product":
                products.append(node)
    return products


def jsonld_selected_shade(products: list[dict]) -> str | None:
    """NARS JSON-LD carries the selected shade as additionalProperty
    {"name": "shade", "value": ..., "description": "Selected color"}."""
    for product in products:
        for prop in product.get("additionalProperty", []):
            if isinstance(prop, dict) and prop.get("name") == "shade":
                value = prop.get("value")
                return str(value) if value is not None else None
    return None
