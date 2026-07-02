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

_JSONLD_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL)
_STATE_ANCHORS = ("var productCache =", "pdpdata =")


def _scan_balanced(text: str, anchor: str, open_char: str, close_char: str) -> str | None:
    """The balanced {...} or [...] JSON literal after `anchor`, via a
    string-aware bracket scan (values may contain brackets inside strings)."""
    i = text.find(anchor)
    if i == -1:
        return None
    start = text.find(open_char, i + len(anchor))
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
        elif ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return text[start : pos + 1]
    return None


def extract_json_object(text: str, anchor: str) -> dict | None:
    literal = _scan_balanced(text, anchor, "{", "}")
    if literal is None:
        return None
    try:
        return json.loads(literal)
    except json.JSONDecodeError:
        return None


def extract_json_array(text: str, anchor: str) -> list | None:
    literal = _scan_balanced(text, anchor, "[", "]")
    if literal is None:
        return None
    try:
        return json.loads(literal)
    except json.JSONDecodeError:
        return None


def parse_sfcc_product_state(html: str) -> dict | None:
    """The SFRA product-state object from a PDP or a variation partial."""
    for anchor in _STATE_ANCHORS:
        state = extract_json_object(html, anchor)
        if state is not None and "ID" in state:
            return state
    return None


def parse_jsonld_products(html: str) -> list[dict]:
    """All JSON-LD Product/ProductGroup nodes, flattening @graph containers
    and descending into ProductGroup.hasVariant (Lookfantastic's shape)."""
    products = []
    for match in _JSONLD_RE.finditer(html):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else data.get("@graph", [data])
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("@type") in ("Product", "ProductGroup"):
                products.append(node)
                for variant in node.get("hasVariant", []):
                    if isinstance(variant, dict) and variant.get("@type") == "Product":
                        products.append(variant)
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
