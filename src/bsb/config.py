"""Config loading for rules, brands and header synonyms.

Defaults to the repo-level config/ directory; the CLI can point elsewhere via
--config. All rule tables are data, never code, so a guide version bump is a
config change (build kit: boozt_rules.yaml is versioned to Guide v1.3).
"""

from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def load_yaml(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_rules(config_dir: Path = DEFAULT_CONFIG_DIR) -> dict:
    rules = load_yaml(config_dir / "boozt_rules.yaml")
    # global colour-word map (Stage 1 color-code proposer) rides inside rules so
    # color_code_for reaches it without a signature change. Optional file.
    word_map_path = config_dir / "color_word_map.yaml"
    if word_map_path.exists():
        rules["color_word_map"] = load_yaml(word_map_path)
    return rules


def load_brands(config_dir: Path = DEFAULT_CONFIG_DIR) -> dict:
    brands = load_yaml(config_dir / "brands.yaml")
    # merge Felina-confirmed lexicon entries (grown by `bsb ingest-review`) from
    # config/lexicons/{brand}.yaml, kept separate so brands.yaml stays a curated,
    # comment-rich file the tool never rewrites. Machine entries append to the
    # brand's shade_lexicon; a shade already present in brands.yaml wins (curated
    # entries are never shadowed).
    lex_dir = config_dir / "lexicons"
    if lex_dir.is_dir():
        for brand_key, brand_cfg in brands.items():
            lex_path = lex_dir / f"{brand_key}.yaml"
            if not lex_path.exists():
                continue
            curated = brand_cfg.get("shade_lexicon") or []
            existing = {str(e.get("shade", "")).casefold() for e in curated}
            merged = list(curated)
            for entry in (load_yaml(lex_path) or {}).get("entries") or []:
                if str(entry.get("shade", "")).casefold() not in existing:
                    merged.append(entry)
            if merged:
                brand_cfg["shade_lexicon"] = merged
    return brands


def load_header_synonyms(config_dir: Path = DEFAULT_CONFIG_DIR) -> dict[str, list[str]]:
    return load_yaml(config_dir / "header_synonyms.yaml")


def load_order_overrides(order_number: str, config_dir: Path = DEFAULT_CONFIG_DIR) -> list[dict]:
    """Per-order manual decisions from config/order_overrides/{order}.yaml
    (empty list when the file does not exist)."""
    path = config_dir / "order_overrides" / f"{order_number}.yaml"
    if not path.exists():
        return []
    data = load_yaml(path) or {}
    return list(data.get("overrides") or [])


_ORDER_CODE = __import__("re").compile(r"^OR\d{2}BZ([A-Z]+?)\d+$")


def brand_for_order(order_number: str | None, brands: dict) -> str | None:
    """Boozt order numbers embed the brand code (OR26BZ{code}0001) — map it
    back through brands.yaml boozt_code entries. BZ orders only."""
    if not order_number:
        return None
    match = _ORDER_CODE.match(order_number.strip().upper())
    if not match:
        return None
    code = match.group(1)
    for key, cfg in brands.items():
        if str(cfg.get("boozt_code", "")).upper() == code:
            return key
    return None
