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
    return load_yaml(config_dir / "boozt_rules.yaml")


def load_brands(config_dir: Path = DEFAULT_CONFIG_DIR) -> dict:
    return load_yaml(config_dir / "brands.yaml")


def load_header_synonyms(config_dir: Path = DEFAULT_CONFIG_DIR) -> dict[str, list[str]]:
    return load_yaml(config_dir / "header_synonyms.yaml")
