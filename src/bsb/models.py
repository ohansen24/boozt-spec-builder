"""Core data contracts from build kit section 5.

`ProductRecord.extras` is a Phase 0 addition on top of the section 5 contract:
it carries auxiliary template fields (length, variation, purchase_price,
expiry_on_pack) that the output sheet needs but that are not first-class
pipeline fields. Everything else matches the kit verbatim.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

FieldStatus = Literal["VERIFIED", "SINGLE_SOURCE", "CONFLICT", "NOT_FOUND", "MANUAL", "ODM_SOURCED"]

SourceMethod = Literal["jsonld", "sfcc_api", "dom", "llm_extract", "odm", "msds", "override"]


class SourceRef(BaseModel):
    url: str
    method: SourceMethod
    fetched_at: datetime
    snippet: str = ""


class FieldValue(BaseModel):
    value: str | None = None
    status: FieldStatus = "NOT_FOUND"
    primary: SourceRef | None = None
    secondary: SourceRef | None = None
    notes: str = ""


class ProductRecord(BaseModel):
    ean12: str  # as in ODM and final sheet
    gtin13: str  # "0" + ean12, used for site lookups
    brand: str
    style_name: FieldValue = Field(default_factory=FieldValue)
    color_name: FieldValue = Field(default_factory=FieldValue)
    size: FieldValue = Field(default_factory=FieldValue)
    ingredients: FieldValue = Field(default_factory=FieldValue)
    gender: FieldValue = Field(default_factory=FieldValue)
    category: FieldValue = Field(default_factory=FieldValue)
    color_code: FieldValue = Field(default_factory=FieldValue)
    flammable: FieldValue = Field(default_factory=FieldValue)
    style_number: FieldValue = Field(default_factory=FieldValue)
    country_iso: FieldValue = Field(default_factory=FieldValue)
    dg: dict | None = None  # DG block, Phase 1
    odm_hints: dict = Field(default_factory=dict)  # name, size, unit, coo, qty, price, subcategory
    extras: dict[str, FieldValue] = Field(default_factory=dict)

    @classmethod
    def field_values(cls) -> list[str]:
        """The FieldValue-typed fields above, in output order (emit + provenance)."""
        return [
            "style_name",
            "style_number",
            "color_name",
            "size",
            "gender",
            "category",
            "color_code",
            "ingredients",
            "flammable",
            "country_iso",
        ]
