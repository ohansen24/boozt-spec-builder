"""Ingest-review correction loop (Oli 2026-07-06): grow the brand color-code
lexicon from Felina's confirmed/corrected values; report per-source correction
rate. Felina's real sheets aren't back yet — this simulates confirm/correct/
fill and checks the lexicon write + merge + metric.
"""

import openpyxl
import yaml

from bsb.config import load_brands
from bsb.ingest.review import ingest_review

PROV_HEADER = ["ean", "field", "value", "status", "primary_url", "secondary_url",
               "method", "snippet", "notes"]


def _reviewed_workbook(path, rows):
    """rows: list of (ean, color_name, her_code, our_code, notes)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data sheet"
    ws.append(["EAN Code", "Color Name", "Boozt Color code"])
    for ean, cn, her, _our, _notes in rows:
        ws.append([ean, cn, her])
    prov = wb.create_sheet("Provenance")
    prov.append(PROV_HEADER)
    for ean, _cn, _her, our, notes in rows:
        prov.append([ean, "color_code", our, "", "", "", "", "", notes])
    wb.save(path)


def test_ingest_grows_lexicon_and_reports_correction_rate(tmp_path):
    # a minimal config dir with a benefit brand and no prior lexicon
    (tmp_path / "brands.yaml").write_text(yaml.safe_dump({"benefit": {"display_name": "Benefit"}}))
    reviewed = tmp_path / "OR26BZFT0001_review.xlsx"
    _reviewed_workbook(reviewed, [
        # confirmed unchanged word proposal -> accepted(word_only)
        ("1", "Wild Plum", 1008, 1008, "proposed from shade name [color_word:plum->1008]"),
        # two-signal proposal she corrected -> corrected(two_signal)
        ("2", "Rebel Brown", 1012, 1010, "two signals agree (shade word + swatch hex)"),
        # hex-only proposal confirmed -> accepted(hex_only)
        ("3", "Hoola", 1010, 1010, "proposed from swatch hex #8B5A2B — lower confidence"),
        # red disagreement she filled -> felina_decided
        ("4", "Dark Cherry", 1009, None, "color-code signals disagree (...) — needs human"),
        # she left it blank -> skipped
        ("5", "Best Life", None, None, "color-code signals disagree (...) — needs human"),
    ])

    out = ingest_review(reviewed, "benefit", reviewer="Felina", date="2026-07-08",
                        config_dir=tmp_path)

    assert out.accepted["word_only"] == 1
    assert out.accepted["hex_only"] == 1
    assert out.corrected["two_signal"] == 1
    assert out.felina_filled_reds == 1
    # 4 decided (wild plum, rebel brown[corrected=her 1012], hoola, dark cherry); best life blank
    assert len(out.new_entries) == 4
    by_shade = {e["shade"]: e for e in out.new_entries}
    assert by_shade["rebel brown"]["code"] == 1012  # HER value, not our 1010
    assert "corrected_from_two_signal:1010" in by_shade["rebel brown"]["source"]
    assert by_shade["dark cherry"]["source"] == "felina_decided"
    assert by_shade["wild plum"]["decided_by"] == "Felina"

    report = " ".join(out.correction_report()).replace("  ", " ")
    assert "two_signal" in report and "100% correction" in report
    assert "1 accepted, 0 corrected → 0% correction" in report  # word_only

    # lexicon file written + merged by load_brands into shade_lexicon
    lex = yaml.safe_load((tmp_path / "lexicons" / "benefit.yaml").read_text())
    assert len(lex["entries"]) == 4
    brands = load_brands(tmp_path)
    lexicon = {e["shade"]: e["code"] for e in brands["benefit"]["shade_lexicon"]}
    assert lexicon["wild plum"] == 1008 and lexicon["rebel brown"] == 1012


def test_ingest_is_idempotent_and_never_shadows_curated(tmp_path):
    # curated brands.yaml entry must never be overwritten by an ingest
    (tmp_path / "brands.yaml").write_text(yaml.safe_dump(
        {"benefit": {"display_name": "Benefit",
                     "shade_lexicon": [{"shade": "wild plum", "code": 9999}]}}
    ))
    reviewed = tmp_path / "r.xlsx"
    _reviewed_workbook(reviewed, [
        ("1", "Wild Plum", 1008, 1008, "proposed from shade name [color_word:plum->1008]"),
    ])
    out = ingest_review(reviewed, "benefit", config_dir=tmp_path)
    # shade already curated -> skipped, not added
    assert out.skipped_existing == 1 and len(out.new_entries) == 0
    brands = load_brands(tmp_path)
    lexicon = {e["shade"]: e["code"] for e in brands["benefit"]["shade_lexicon"]}
    assert lexicon["wild plum"] == 9999  # curated value preserved


def test_confirmed_lexicon_entry_maps_deterministically_next_order(tmp_path):
    # after ingest, the shade resolves via lexicon (confirmed) — not a proposal
    from bsb.categorize.rules import color_code_for
    from bsb.config import load_rules

    (tmp_path / "brands.yaml").write_text(yaml.safe_dump({"benefit": {"display_name": "Benefit"}}))
    reviewed = tmp_path / "r.xlsx"
    _reviewed_workbook(reviewed, [
        ("1", "Wild Plum", 1008, 1008, "proposed from shade name [color_word:plum->1008]"),
    ])
    ingest_review(reviewed, "benefit", config_dir=tmp_path)
    benefit = load_brands(tmp_path)["benefit"]
    d = color_code_for("Makeup", "Wild Plum", load_rules(), benefit)
    assert d.code == 1008 and d.rule.startswith("lexicon:") and not d.proposal
