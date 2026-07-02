"""bsb CLI (build kit section 7): run, resolve, report.

Phase 0 wires the pure-local pipeline: ingest -> categorize -> normalize ->
emit. Fields that need web sources carry status NOT_FOUND until the resolve /
extract stages exist (Phase 1).
"""

from datetime import UTC, datetime
from pathlib import Path

import click

from bsb.config import DEFAULT_CONFIG_DIR, load_brands, load_header_synonyms, load_rules
from bsb.emit.writer import RunSummary, write_output
from bsb.ingest.odm import parse_odm
from bsb.pipeline import build_records


@click.group()
def main() -> None:
    """Boozt Spec Builder: ODM + blank Boozt template -> filled, validated
    product data sheet with per-field provenance."""


@main.command()
@click.option("--odm", "odm_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--template", "template_path", required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option("--brand", "brand_key", required=True)
@click.option("--out", "out_path", required=True, type=click.Path(dir_okay=False))
@click.option(
    "--config",
    "config_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=DEFAULT_CONFIG_DIR,
    show_default=True,
)
@click.option(
    "--cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("cache"),
    show_default=True,
)
@click.option("--resolve/--no-resolve", "do_resolve", default=False, help="Fetch brand-site data")
@click.option(
    "--validate/--no-validate", "do_validate", default=True, help="Validator pass (with --resolve)"
)
@click.option("--bases", "bases_csv", default=None, help="Comma-separated base-name filter (pilot)")
@click.option("--show-variants", is_flag=True, help="Print per-master variant tables")
@click.option("--sample-provenance", default=0, type=int, help="Print N random provenance rows")
def run(
    odm_path: str,
    template_path: str,
    brand_key: str,
    out_path: str,
    config_dir: Path,
    cache_dir: Path,
    do_resolve: bool,
    do_validate: bool,
    bases_csv: str | None,
    show_variants: bool,
    sample_provenance: int,
) -> None:
    """Fill a Boozt template from an ODM (--resolve adds brand-site data and
    the validator pass; anomalies stop the run before emit)."""
    brands = load_brands(config_dir)
    rules = load_rules(config_dir)
    synonyms = load_header_synonyms(config_dir)

    brand_key = brand_key.lower()
    if brand_key not in brands:
        raise click.BadParameter(f"unknown brand {brand_key!r}; known: {', '.join(sorted(brands))}")
    brand_cfg = brands[brand_key]

    odm = parse_odm(odm_path)
    bases_filter = None
    if bases_csv:
        bases_filter = {b.strip() for b in bases_csv.split(",") if b.strip()}
        odm.rows = [r for r in odm.rows if r.base_name in bases_filter]
        click.echo(f"Base filter: {len(odm.rows)} rows across {len(bases_filter)} bases")

    records = build_records(odm, brand_key, brands, rules, odm_path)

    run_meta = {
        "run at": datetime.now(UTC).isoformat(timespec="seconds"),
        "odm": odm_path,
        "template": template_path,
        "brand": brand_key,
        "guide version": rules["guide_version"],
        "ean length profile": ", ".join(
            f"{length}-digit: {count}" for length, count in sorted(odm.length_profile.items())
        ),
        "_ingest_issues": odm.issues,
    }

    if not do_resolve:
        run_meta["phase"] = "0 (pure local — no web sources; web-dependent fields are NOT_FOUND)"
        summary = write_output(template_path, out_path, records, synonyms, run_meta)
        click.echo("EAN length profile: " + run_meta["ean length profile"])
        _print_summary(summary)
        return

    _run_resolved(
        odm,
        records,
        brand_key,
        brand_cfg,
        rules,
        synonyms,
        template_path,
        out_path,
        cache_dir,
        run_meta,
        do_validate,
        show_variants,
        sample_provenance,
    )


def _run_resolved(
    odm,
    records,
    brand_key,
    brand_cfg,
    rules,
    synonyms,
    template_path,
    out_path,
    cache_dir,
    run_meta,
    do_validate,
    show_variants,
    sample_provenance,
) -> None:
    import random

    from bsb.fetch.cache import EanCache, HttpCache
    from bsb.fetch.ladder import PlaywrightSession, PoliteFetcher
    from bsb.pipeline import apply_resolution
    from bsb.resolve.adapters.nars import NarsAdapter
    from bsb.resolve.orchestrator import resolve_order
    from bsb.resolve.validators import IncidecoderWeak, LookfantasticValidator, cache_lf_hit

    if brand_cfg.get("adapter") != "nars_sfcc":
        raise click.ClickException(f"--resolve: no adapter configured for brand {brand_key!r}")

    http_cache = HttpCache(cache_dir)
    ean_cache = EanCache(cache_dir)
    fetcher = PoliteFetcher(http_cache)
    playwright = PlaywrightSession(http_cache, fetcher.limiter)
    adapter = NarsAdapter(fetcher, brand_cfg, ean_cache, playwright)

    try:
        click.echo(f"Resolving {len(odm.rows)} EANs master-first…")
        resolution = resolve_order(odm, adapter, progress=lambda m: click.echo("  " + m))
        counts = resolution.counts()
        click.echo(
            f"Resolve done: {counts['resolved_ok']}/{counts['eans']} ok, "
            f"{counts['masters_found']} masters"
        )

        if resolution.blocking_anomalies:
            click.echo("\n!! BLOCKING ANOMALIES — stopping before validators/emit:")
            for a in resolution.anchor_rejections:
                click.echo(f"  anchor rejection: {a}")
            for a in resolution.missing_shades:
                click.echo(f"  missing shade:    {a}")
            raise SystemExit(2)
        for failure in resolution.master_failures:
            click.echo(f"  (non-blocking) master not found: {failure}")
        for warning in resolution.swatch_warnings:
            click.echo(f"  (warning) {warning}")
        if resolution.swatch_warnings:
            run_meta["swatch warnings"] = " | ".join(resolution.swatch_warnings)

        lf_by_base: dict = {}
        inci_by_base: dict = {}
        if do_validate:
            lf = LookfantasticValidator(fetcher, playwright)
            inci_weak = IncidecoderWeak(fetcher)
            click.echo(f"\nValidator pass over {len(resolution.masters)} masters…")
            for base, master in resolution.masters.items():
                first_ean = next(r.ean12 for r in odm.rows if r.base_name == base)
                product = lf.find_product(first_ean)
                lf_by_base[base] = product
                click.echo(
                    f"  LF {base}: "
                    + (
                        f"{len(product.by_barcode)} barcodes @ {product.url}"
                        if product
                        else "no hit"
                    )
                )
                weak = inci_weak.find_inci(
                    str(brand_cfg.get("display_name", brand_key)), master.product_name
                )
                inci_by_base[base] = weak

        conflict_cells = 0
        compared_cells = 0
        size_anomalies: list[str] = []
        by_ean_row = {r.ean12: r for r in odm.rows}
        for record in records:
            row = by_ean_row[record.ean12]
            resolved = resolution.by_ean.get(record.ean12)
            lf_product = lf_by_base.get(row.base_name)
            weak = inci_by_base.get(row.base_name)
            size_anomalies += apply_resolution(
                record, row, resolved, brand_cfg, rules, lf_product, weak
            )
            for field in ("style_name", "color_name", "size"):
                fv = getattr(record, field)
                if fv.secondary is not None or fv.status == "CONFLICT":
                    compared_cells += 1
                    if fv.status == "CONFLICT":
                        conflict_cells += 1

            if lf_product is not None and row.ean12 in lf_product.by_barcode:
                cache_lf_hit(ean_cache, row.gtin13, lf_product, row.ean12)

        if size_anomalies:
            click.echo("\n!! SIZE ANOMALIES vs ODM hints — stopping before emit:")
            for a in size_anomalies:
                click.echo(f"  {a}")
            raise SystemExit(2)

        conflict_rate = (conflict_cells / compared_cells) if compared_cells else 0.0
        click.echo(
            f"\nValidator comparisons: {compared_cells} cells, {conflict_cells} conflicts "
            f"({conflict_rate:.1%})"
        )
        if conflict_rate > 0.10:
            click.echo("!! validator conflict rate above 10% — stopping before emit")
            raise SystemExit(2)

        if show_variants:
            _print_variant_tables(resolution, lf_by_base, odm)

        shade_fmt = brand_cfg.get("shade_format") or {}
        if shade_fmt.get("pending_note"):
            run_meta["shade format"] = shade_fmt["pending_note"]
        run_meta["phase"] = "1 (brand adapter + validator pass)"
        run_meta["validators"] = (
            "lookfantastic (GTIN-anchored), incidecoder (weak, notes only); "
            "douglas/boots/flaconi/sephora disabled: bot-walled at httpx and Playwright rungs"
        )
        run_meta["inci casing"] = "as published by brand (ALL CAPS); separators normalized to comma"

        summary = write_output(template_path, out_path, records, synonyms, run_meta)
        _print_summary(summary)
        _print_queue_by_reason(summary)
        if sample_provenance:
            _print_provenance_sample(records, sample_provenance, random.Random(20260702))
    finally:
        fetcher.close()
        playwright.close()


def _print_variant_tables(resolution, lf_by_base, odm) -> None:
    click.echo("\n=== per-master variant tables ===")
    for base, master in resolution.masters.items():
        lf = lf_by_base.get(base)
        click.echo(
            f"\nmaster {master.master_id}  ({master.product_name})  "
            f"{len(master.shade_by_gtin)} shades on site"
        )
        click.echo(f"  pdp  {master.pdp_url}")
        if master.inci_text:
            click.echo(
                f"  inci {master.inci_text[:100]}… (shade {master.inci_selected_gtin} selected)"
            )
        if lf:
            click.echo(f"  LF   {lf.url} ({len(lf.by_barcode)} barcodes)")
        for row in [r for r in odm.rows if r.base_name == base]:
            entry = resolution.by_ean.get(row.ean12)
            variant = entry.variant if entry else None
            lf_variant = lf.by_barcode.get(row.ean12) if lf else None
            status = "ok" if entry and entry.ok else f"FAIL: {entry.error if entry else '?'}"
            shade = variant.shade if variant else "-"
            size = variant.size_text if variant else "-"
            lf_note = f" | LF: {lf_variant.shade}" if lf_variant else " | LF: -"
            click.echo(f"    {row.ean12}  {status:4} shade={shade!r} size={size!r}{lf_note}")


def _print_queue_by_reason(s: RunSummary) -> None:
    def reason_of(item) -> str:
        note = (item.notes or "").split(";")[0].strip()
        return f"{item.field}: {note[:70] if note else item.status}"

    click.echo("\nReview queue by reason:")
    groups: dict[str, int] = {}
    for item in s.review_red + s.review_yellow:
        key = ("RED " if item in s.review_red else "YEL ") + reason_of(item)
        groups[key] = groups.get(key, 0) + 1
    for key, count in sorted(groups.items(), key=lambda kv: (-kv[1], kv[0]))[:20]:
        click.echo(f"  {count:4d}  {key}")


def _print_provenance_sample(records, n: int, rng) -> None:
    click.echo(f"\n{n} random provenance rows (browser spot-checks):")
    pool = []
    for record in records:
        from bsb.models import ProductRecord

        for field in ProductRecord.field_values():
            fv = getattr(record, field)
            if fv.value and fv.primary:
                pool.append((record.ean12, field, fv))
    for ean, field, fv in rng.sample(pool, min(n, len(pool))):
        click.echo(f"  {ean} {field} = {fv.value!r} [{fv.status}]")
        click.echo(f"      primary   {fv.primary.url}")
        if fv.secondary:
            click.echo(f"      secondary {fv.secondary.url}")
        click.echo(f"      snippet   {fv.primary.snippet[:100]}")


def _print_summary(s: RunSummary) -> None:
    click.echo(f"\nWrote {s.out_path} ({s.records} rows)")
    if s.cleared_template_rows:
        click.echo(
            f"  ! template contained {s.cleared_template_rows} pre-existing data rows — "
            "cleared in the output copy"
        )
    for issue in s.ingest_issues:
        click.echo(f"  ! ingest: {issue}")

    click.echo("\nStatus totals (written cells):")
    for status, count in sorted(s.status_totals.items()):
        click.echo(f"  {status:14} {count}")

    click.echo("\nCategories:")
    for category, count in sorted(s.category_totals.items(), key=lambda kv: -kv[1]):
        click.echo(f"  {category:20} {count}")

    click.echo(
        f"\nReview queue: {len(s.review_red)} red, {len(s.review_yellow)} yellow "
        "(see 'Run report' sheet)"
    )
    by_field: dict[str, int] = {}
    for item in s.review_red:
        by_field[item.field] = by_field.get(item.field, 0) + 1
    if by_field:
        click.echo("  red by field: " + ", ".join(f"{f}={n}" for f, n in sorted(by_field.items())))
    yellow_by_field: dict[str, int] = {}
    for item in s.review_yellow:
        yellow_by_field[item.field] = yellow_by_field.get(item.field, 0) + 1
    if yellow_by_field:
        click.echo(
            "  yellow by field: "
            + ", ".join(f"{f}={n}" for f, n in sorted(yellow_by_field.items()))
        )
    if s.unknown_headers:
        click.echo("\nUnknown template headers (left untouched): " + "; ".join(s.unknown_headers))
    if s.missing_fields:
        click.echo(
            "Canonical fields absent from template (skipped): " + "; ".join(s.missing_fields)
        )


@main.command()
@click.option("--gtin", required=True, help="GTIN-13 (0 + ODM ean12)")
@click.option("--brand", "brand_key", required=True)
@click.option("-v", "--verbose", is_flag=True)
@click.option(
    "--config",
    "config_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=DEFAULT_CONFIG_DIR,
    show_default=True,
)
@click.option(
    "--cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("cache"),
    show_default=True,
)
def resolve(gtin: str, brand_key: str, verbose: bool, config_dir: Path, cache_dir: Path) -> None:
    """Single-item debug resolve: master discovery + GTIN-anchored variation."""
    from bsb.fetch.cache import EanCache, HttpCache
    from bsb.fetch.ladder import PlaywrightSession, PoliteFetcher
    from bsb.resolve.adapters.nars import NarsAdapter

    brands = load_brands(config_dir)
    brand_key = brand_key.lower()
    brand_cfg = brands.get(brand_key)
    if not brand_cfg or brand_cfg.get("adapter") != "nars_sfcc":
        raise click.BadParameter(f"no adapter configured for brand {brand_key!r}")

    http_cache = HttpCache(cache_dir)
    fetcher = PoliteFetcher(http_cache)
    playwright = PlaywrightSession(http_cache, fetcher.limiter)
    adapter = NarsAdapter(fetcher, brand_cfg, EanCache(cache_dir), playwright)

    try:
        master = adapter.discover_master(gtin)
        click.echo(f"master     {master.master_id}  ({master.product_name})")
        click.echo(f"  pdp      {master.pdp_url}" + ("  [cache]" if master.from_cache else ""))
        click.echo(f"  shades   {len(master.shade_by_gtin)} in swatch list")
        click.echo(f"  size     {master.size_text!r}")
        if master.inci_text:
            shown = master.inci_text if verbose else master.inci_text[:120] + "…"
            click.echo(
                f"  inci     {shown} (captured with shade {master.inci_selected_gtin} selected)"
            )
        if verbose:
            for g, shade in sorted(master.shade_by_gtin.items()):
                click.echo(f"           {g}  {shade}")

        variant = adapter.resolve_variant(master, gtin)
        click.echo(
            f"\nvariant    {variant.gtin13}  ok={variant.ok}  via={variant.via}"
            + ("  [cache]" if variant.from_cache else "")
        )
        if variant.ok:
            click.echo(f"  name     {variant.product_name}")
            click.echo(f"  shade    {variant.shade}")
            click.echo(f"  size     {variant.size_text!r}")
            click.echo(f"  anchor   returned ID == requested GTIN ({variant.returned_id})")
        else:
            click.echo(f"  REJECTED: {variant.reject_reason}")
        click.echo(f"  url      {variant.url}")
        click.echo(f"  snippet  {variant.snippet}")
    finally:
        fetcher.close()
        playwright.close()


@main.command()
@click.option("--run", "run_dir", required=True, type=click.Path(exists=True))
def report(run_dir: str) -> None:
    """Re-print the report for a cached run (Phase 1)."""
    raise click.ClickException("Phase 1: cached runs are not implemented yet")


if __name__ == "__main__":
    main()
