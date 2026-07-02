from pathlib import Path

import pytest

from bsb.config import load_brands, load_header_synonyms, load_rules

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"
ODM_PATH = REPO / "data" / "inbox" / "OR26BZQN0001_ODM.xlsx"
TEMPLATE_PATH = REPO / "data" / "inbox" / "blank_template.xlsx"


@pytest.fixture(scope="session")
def rules() -> dict:
    return load_rules()


@pytest.fixture(scope="session")
def brands() -> dict:
    return load_brands()


@pytest.fixture(scope="session")
def synonyms() -> dict:
    return load_header_synonyms()
