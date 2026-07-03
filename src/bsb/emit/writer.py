"""Output writer (build kit 6.9): a colored copy of the uploaded template plus
"Provenance" and "Run report" sheets.

The copy is header-mapped (never column letters), the EAN column is formatted
as text, and every pre-existing data row in the template is cleared first —
uploaded "blank" templates have been observed carrying leftover rows from
previous orders. Fill colors: green = VERIFIED / ODM_SOURCED, yellow =
SINGLE_SOURCE or needs-attention MANUAL, red = CONFLICT / NOT_FOUND.
"""

import shutil
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import PatternFill
from pydantic import BaseModel, Field

from bsb.ingest.template import TemplateMap, map_headers
from bsb.models import FieldValue, ProductRecord

GREEN = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
YELLOW = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
RED = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")

_STATUS_RANK = {"CONFLICT": 0, "NOT_FOUND": 0, "SINGLE_SOURCE": 1, "MANUAL": 1}


class ReviewItem(BaseModel):
    ean: str
    field: str
    status: str
    value: str | None = None
    notes: str = ""


class RunSummary(BaseModel):
    out_path: str
    records: int
    status_totals: dict[str, int] = Field(default_factory=dict)
    category_totals: dict[str, int] = Field(default_factory=dict)
    review_red: list[ReviewItem] = Field(default_factory=list)
    review_yellow: list[ReviewItem] = Field(default_factory=list)
    cleared_template_rows: int = 0
    unknown_headers: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    ingest_issues: list[str] = Field(default_factory=list)
    verify_at_receipt: list[ReviewItem] = Field(default_factory=list)


def _by_design_blank(fv: FieldValue) -> bool:
    """MANUAL blanks whose notes start with "by design:" are intentional
    (e.g. style number issued by Boozt after receipt) — no fill, no review."""
    return fv.status == "MANUAL" and not fv.value and fv.notes.startswith("by design:")


def fill_for(fv: FieldValue) -> PatternFill | None:
    if fv.status in ("VERIFIED", "ODM_SOURCED"):
        return GREEN
    if fv.status == "SINGLE_SOURCE":
        return YELLOW
    if fv.status in ("CONFLICT", "NOT_FOUND"):
        return RED
    if fv.status == "MANUAL":
        return None if fv.value or _by_design_blank(fv) else YELLOW
    return None


def _cell_value(field: str, fv: FieldValue) -> object:
    if fv.value is None:
        return None
    if field == "color_code":
        try:
            return int(fv.value)
        except ValueError:
            return fv.value
    if field == "purchase_price":
        try:
            return float(fv.value)
        except ValueError:
            return fv.value
    return fv.value


def _clear_data_rows(ws, tmap: TemplateMap) -> int:
    """Remove pre-existing data rows below the header from the output copy."""
    ean_col = tmap.columns.get("ean")
    cleared = 0
    if ean_col is not None:
        for row in ws.iter_rows(min_row=tmap.header_row + 1):
            if row[ean_col - 1].value not in (None, ""):
                cleared += 1
    if ws.max_row > tmap.header_row:
        ws.delete_rows(tmap.header_row + 1, ws.max_row - tmap.header_row)
    return cleared


def write_output(
    template_path: str | Path,
    out_path: str | Path,
    records: list[ProductRecord],
    synonyms: dict[str, list[str]],
    run_meta: dict,
) -> RunSummary:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template_path, out_path)

    wb = load_workbook(out_path)
    ws = wb.worksheets[0]
    tmap = map_headers(ws, synonyms)
    if not tmap.has("ean"):
        raise ValueError(f"{template_path}: no EAN column found via header synonyms")

    cleared = _clear_data_rows(ws, tmap)

    status_counter: Counter[str] = Counter()
    category_counter: Counter[str] = Counter()
    review: list[tuple[int, ReviewItem]] = []
    provenance_rows: list[list[object]] = []

    for i, record in enumerate(records):
        row_no = tmap.header_row + 1 + i
        ean_cell = ws.cell(row=row_no, column=tmap.columns["ean"])
        ean_cell.value = record.ean12
        ean_cell.number_format = "@"

        if tmap.has("brand"):
            ws.cell(row=row_no, column=tmap.columns["brand"]).value = record.brand

        fields: list[tuple[str, FieldValue]] = [
            (name, getattr(record, name)) for name in ProductRecord.field_values()
        ]
        fields += list(record.extras.items())

        for field, fv in fields:
            in_template = tmap.has(field)
            if in_template:
                cell = ws.cell(row=row_no, column=tmap.columns[field])
                cell.value = _cell_value(field, fv)
                fill = fill_for(fv)
                if fill is not None:
                    cell.fill = fill

                status_counter[fv.status] += 1
                rank = _STATUS_RANK.get(fv.status)
                settled_manual = fv.status == "MANUAL" and (fv.value or _by_design_blank(fv))
                if rank is not None and not settled_manual:
                    review.append(
                        (
                            rank,
                            ReviewItem(
                                ean=record.ean12,
                                field=field,
                                status=fv.status,
                                value=fv.value,
                                notes=fv.notes,
                            ),
                        )
                    )

            provenance_rows.append(
                [
                    record.ean12,
                    field,
                    fv.value,
                    fv.status,
                    fv.primary.url if fv.primary else None,
                    fv.secondary.url if fv.secondary else None,
                    fv.primary.method if fv.primary else None,
                    fv.primary.snippet if fv.primary else None,
                    fv.notes
                    if in_template
                    else f"{fv.notes} [column absent from template]".strip(),
                ]
            )

        if record.category.value:
            category_counter[record.category.value] += 1
        else:
            category_counter["(uncategorized)"] += 1

    prov = wb.create_sheet("Provenance")
    prov.append(
        [
            "ean",
            "field",
            "value",
            "status",
            "primary_url",
            "secondary_url",
            "method",
            "snippet",
            "notes",
        ]
    )
    for row in provenance_rows:
        prov.append(row)
    for cell in prov["A"]:
        cell.number_format = "@"

    review.sort(key=lambda item: (item[0], item[1].ean, item[1].field))
    review_red = [r for rank, r in review if rank == 0]
    review_yellow = [r for rank, r in review if rank == 1]

    # receiving checklist: cells flagged for confirmation against physical
    # goods at warehouse receipt (Felina's checkpoint), independent of color
    receipt_checks = [
        ReviewItem(ean=r.ean12, field=f, status=fv.status, value=fv.value, notes=fv.notes)
        for r in records
        for f, fv in (
            [(name, getattr(r, name)) for name in ProductRecord.field_values()]
            + list(r.extras.items())
        )
        if "VERIFY_AT_RECEIPT" in fv.notes
    ]

    report = wb.create_sheet("Run report")
    for key, value in run_meta.items():
        if key.startswith("_"):
            continue
        report.append([key, str(value)])
    report.append(["records", len(records)])
    report.append(["cleared pre-existing template data rows", cleared])
    if tmap.unknown_headers:
        report.append(
            ["unknown headers (left untouched)", "; ".join(h for _, h in tmap.unknown_headers)]
        )
    if tmap.missing_fields:
        report.append(
            ["canonical fields absent from template (skipped)", "; ".join(tmap.missing_fields)]
        )
    for issue in run_meta.get("_ingest_issues", []):
        report.append(["ingest issue", issue])
    report.append([])
    report.append(["status", "cells"])
    for status, count in sorted(status_counter.items()):
        report.append([status, count])
    report.append([])
    report.append(["category", "rows"])
    for category, count in sorted(category_counter.items()):
        report.append([category, count])
    if receipt_checks:
        report.append([])
        report.append(["VERIFY AT RECEIPT — confirm against physical goods (warehouse)"])
        report.append(["ean", "field", "value", "notes"])
        for item in receipt_checks:
            report.append([item.ean, item.field, item.value, item.notes])
    report.append([])
    report.append(["review queue (red first, then yellow)"])
    report.append(["ean", "field", "status", "value", "notes"])
    for item in review_red + review_yellow:
        report.append([item.ean, item.field, item.status, item.value, item.notes])

    wb.save(out_path)

    return RunSummary(
        out_path=str(out_path),
        records=len(records),
        status_totals=dict(status_counter),
        category_totals=dict(category_counter),
        review_red=review_red,
        review_yellow=review_yellow,
        cleared_template_rows=cleared,
        unknown_headers=[h for _, h in tmap.unknown_headers],
        missing_fields=tmap.missing_fields,
        ingest_issues=list(run_meta.get("_ingest_issues", [])),
        verify_at_receipt=receipt_checks,
    )
