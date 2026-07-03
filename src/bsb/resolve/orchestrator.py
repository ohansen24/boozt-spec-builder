"""Master-first order resolution (build kit 6.3 shade-family efficiency).

Groups ODM rows by base name, discovers each master once (trying successive
group EANs if the first PDP is missing), checks every group EAN against the
master's swatch list, then resolves each EAN through the GTIN-anchored
Product-Variation call.

Anomalies are collected, never swallowed:
- anchor_rejections: variation payload returned a different variant id
- missing_shades:    ODM EAN absent from the discovered master's swatch list
- master_failures:   no PDP found for any EAN of a base-name group (these
                     rows go NOT_FOUND red; not a stop condition — orders
                     legitimately contain items the EU site no longer lists)
"""

from collections.abc import Callable

from pydantic import BaseModel, Field

from bsb.fetch.ladder import FetchError
from bsb.ingest.odm import OdmParseResult, OdmRow
from bsb.resolve.adapters.nars import MasterResult, NarsAdapter, VariantResult

MAX_DISCOVERY_ATTEMPTS = 3


class ResolvedEan(BaseModel):
    ean12: str
    gtin13: str
    ok: bool = False
    in_swatch_list: bool | None = None
    master: MasterResult | None = None
    variant: VariantResult | None = None
    error: str | None = None


class OrderResolution(BaseModel):
    by_ean: dict[str, ResolvedEan] = Field(default_factory=dict)
    masters: dict[str, MasterResult] = Field(default_factory=dict)  # base_name -> master
    anchor_rejections: list[str] = Field(default_factory=list)
    missing_shades: list[str] = Field(default_factory=list)  # unresolved -> blocking
    swatch_warnings: list[str] = Field(default_factory=list)  # delisted but self-anchored PDP
    master_failures: list[str] = Field(default_factory=list)

    @property
    def blocking_anomalies(self) -> list[str]:
        return self.anchor_rejections + self.missing_shades

    def counts(self) -> dict[str, int]:
        ok = sum(1 for r in self.by_ean.values() if r.ok)
        return {
            "eans": len(self.by_ean),
            "resolved_ok": ok,
            "not_found": len(self.by_ean) - ok,
            "masters_found": len(self.masters),
        }


def _group_by_base(rows: list[OdmRow]) -> dict[str, list[OdmRow]]:
    groups: dict[str, list[OdmRow]] = {}
    for row in rows:
        groups.setdefault(row.base_name, []).append(row)
    return groups


def resolve_order(
    odm: OdmParseResult,
    adapter: NarsAdapter,
    bases_filter: set[str] | None = None,
    progress: Callable[[str], None] = lambda _msg: None,
) -> OrderResolution:
    result = OrderResolution()
    groups = _group_by_base(odm.rows)
    if bases_filter is not None:
        groups = {b: rows for b, rows in groups.items() if b in bases_filter}

    for base, rows in groups.items():
        master: MasterResult | None = None
        last_error = ""
        for candidate in rows[:MAX_DISCOVERY_ATTEMPTS]:
            try:
                master = adapter.discover_master(candidate.gtin13)
                break
            except (FetchError, ValueError) as exc:
                last_error = str(exc)
                progress(f"  discovery via {candidate.gtin13} failed: {exc}")

        if master is None:
            result.master_failures.append(f"{base}: {last_error}")
            for row in rows:
                result.by_ean[row.ean12] = ResolvedEan(
                    ean12=row.ean12,
                    gtin13=row.gtin13,
                    error=f"master not found for base {base!r}: {last_error}",
                )
            progress(f"✗ {base}: no master found ({len(rows)} rows NOT_FOUND)")
            continue

        result.masters[base] = master
        progress(
            f"● {base} -> {master.master_id} ({master.product_name!r}, "
            f"{len(master.shade_by_gtin)} shades)"
        )

        for row in rows:
            entry = ResolvedEan(ean12=row.ean12, gtin13=row.gtin13, master=master)

            if master.is_simple_product:
                # no color dimension: the PDP itself anchors its own GTIN
                entry.in_swatch_list = None
                if master.selected_id == row.gtin13:
                    entry.variant = adapter.variant_from_pdp(master)
                    entry.ok = True
                else:
                    entry.error = (
                        f"simple product PDP anchors {master.selected_id}, not {row.gtin13}"
                    )
                    result.missing_shades.append(f"{row.ean12} ({base}): {entry.error}")
                result.by_ean[row.ean12] = entry
                continue

            entry.in_swatch_list = row.gtin13 in master.shade_by_gtin
            if row.gtin13 not in master.color_val_by_gtin:
                # delisted (absent from the swatch map — LRF "Gobi") or
                # semi-delisted (in the map but missing from the purchasable
                # vals — the Orgasm quad): Product-Variation would only return
                # the master default, but the shade's own PDP may still exist
                # and self-anchor the GTIN.
                kind = (
                    "listed but not purchasable (no color value)"
                    if entry.in_swatch_list
                    else f"not in {master.master_id} swatch list (delisted?)"
                )
                detail = f"{row.ean12} ({base} - {row.shade or '?'})"
                try:
                    own = adapter.discover_master(row.gtin13)
                except (FetchError, ValueError) as exc:
                    own = None
                    fallback_error = str(exc)
                if own is not None and own.selected_id == row.gtin13:
                    if own.inci_text is None and master.inci_text is not None:
                        # delisted PDPs render degraded; the group master's
                        # INCI applies — same master id, one list per product
                        own.inci_text = master.inci_text
                        own.inci_selected_gtin = master.inci_selected_gtin
                    if own.selected_shade is None:
                        own.selected_shade = master.shade_by_gtin.get(row.gtin13)
                    entry.variant = adapter.variant_from_pdp(own)
                    entry.ok = True
                    entry.master = own
                    result.swatch_warnings.append(
                        f"{detail}: {kind} — resolved via own PDP, self-anchored ({own.pdp_url})"
                    )
                    progress(f"  ~ {row.ean12}: {kind}, own PDP self-anchors")
                else:
                    reason = (
                        f"own PDP anchors {own.selected_id}" if own is not None else fallback_error
                    )
                    entry.error = f"{kind}; own-PDP fallback: {reason}"
                    result.missing_shades.append(f"{detail}: {entry.error}")
                result.by_ean[row.ean12] = entry
                continue

            try:
                variant: VariantResult = adapter.resolve_variant(master, row.gtin13)
            except FetchError as exc:
                entry.error = str(exc)
                result.by_ean[row.ean12] = entry
                progress(f"  ✗ {row.ean12}: {exc}")
                continue

            entry.variant = variant
            if variant.ok:
                entry.ok = True
            elif variant.returned_id is None and master.selected_id == row.gtin13:
                # the variation partial served no product state, but the master
                # PDP itself already self-anchors this exact GTIN (seen live:
                # Powermatte Lip Pigment on the US site) — use the PDP evidence
                entry.variant = adapter.variant_from_pdp(master)
                entry.ok = True
                progress(f"  ~ {row.ean12}: variation partial unparseable, PDP self-anchors")
            else:
                entry.error = variant.reject_reason
                if variant.returned_id is not None:
                    result.anchor_rejections.append(f"{row.ean12}: {variant.reject_reason}")
            result.by_ean[row.ean12] = entry

    return result
