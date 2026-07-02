"""Section 5 contracts stay stable — downstream stages depend on them."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from bsb.models import FieldValue, ProductRecord, SourceRef


def test_field_value_defaults_fail_closed():
    fv = FieldValue()
    assert fv.value is None
    assert fv.status == "NOT_FOUND"
    assert fv.primary is None and fv.secondary is None


def test_status_enum_is_closed():
    with pytest.raises(ValidationError):
        FieldValue(status="GUESSED")


def test_source_method_enum_is_closed():
    with pytest.raises(ValidationError):
        SourceRef(url="x", method="wikipedia", fetched_at=datetime.now(UTC))


def test_product_record_contract_fields():
    record = ProductRecord(ean12="194251026404", gtin13="0194251026404", brand="NARS")
    for field in ProductRecord.field_values():
        assert isinstance(getattr(record, field), FieldValue)
    assert record.dg is None
    assert record.odm_hints == {}
    # independent instances must not share mutable defaults
    other = ProductRecord(ean12="194251143040", gtin13="0194251143040", brand="NARS")
    record.odm_hints["x"] = 1
    assert other.odm_hints == {}
