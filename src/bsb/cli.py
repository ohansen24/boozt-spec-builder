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
def run(odm_path: str, template_path: str, brand_key: str, out_path: str, config_dir: Path) -> None:
    """Fill a Boozt template from an ODM (Phase 0: pure local)."""
    brands = load_brands(config_dir)
    rules = load_rules(config_dir)
    synonyms = load_header_synonyms(config_dir)

    brand_key = brand_key.lower()
    if brand_key not in brands:
        raise click.BadParameter(f"unknown brand {brand_key!r}; known: {', '.join(sorted(brands))}")

    odm = parse_odm(odm_path)
    records = build_records(odm, brand_key, brands, rules, odm_path)

    run_meta = {
        "run at": datetime.now(UTC).isoformat(timespec="seconds"),
        "odm": odm_path,
        "template": template_path,
        "brand": brand_key,
        "guide version": rules["guide_version"],
        "phase": "0 (pure local — no web sources; web-dependent fields are NOT_FOUND)",
        "ean length profile": ", ".join(
            f"{length}-digit: {count}" for length, count in sorted(odm.length_profile.items())
        ),
        "_ingest_issues": odm.issues,
    }
    summary = write_output(template_path, out_path, records, synonyms, run_meta)
    click.echo("EAN length profile: " + run_meta["ean length profile"])
    _print_summary(summary)


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
