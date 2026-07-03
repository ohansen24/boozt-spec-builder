# Boozt Spec Builder, Build Kit v1

Purpose: a pipeline that turns a Buying Labs ODM plus a blank Boozt data template into a filled, validated Boozt product data sheet, with per-field provenance and a review queue. Built as a Python library with a CLI, developed and run via Claude Code first, wrapped for self-serve later. First target: NARS order OR26BZQN0001 (119 EANs).

## 1. Current workflow and observed failure modes

Today the sheet is filled manually per order (roughly 2 days for 119 rows). The provided files contain real errors that the tool must catch as lint rules (these are the regression test cases):

1. NARS WIP row 2, Style number = "SVR3662361001699": an SVR prefix plus a wrong EAN pasted into a NARS order.
2. Boozt Product Category = " Makeup " containing non-breaking spaces.
3. Size = "4,4gr": comma decimal and unit "gr" instead of the guide format "4.4 g".
4. Color Name = "Orgasm " with a trailing space.
5. Inconsistent no-color conventions across finished sheets: CLEAR (SVR, Aesop) vs NO COLOR vs NO SHADE (Olaplex).

A second structural finding: the three finished sheets do NOT share one column layout. SVR/NARS have "Boozt Color code" in K and DG columns T to Y; Olaplex has no color code column at all and carries order columns W to AC; Aesop adds four description languages and a fuller DG block through AH. Conclusion: never address columns by letter. Map by header name.

## 2. Non-negotiable design principles (anti-hallucination charter)

1. Provenance or it does not ship. Every auto-filled cell traces to a source URL, retrieval method, timestamp, and evidence snippet.
2. GTIN-anchored acceptance. A source may only be used for an EAN if that exact GTIN (12 or 13 digit form) is present in the source's structured data or visible page content. Never adopt a "best match".
3. Extraction over generation. Values are parsed from structured payloads (JSON-LD, platform APIs, labeled DOM). Where an LLM is used, it maps fetched text into a strict schema and must return a verbatim evidence snippet per field; the code verifies the snippet is a substring of the fetched source, otherwise the extraction is rejected.
4. Enums fail closed. Product Category, Color Code, Gender, Flammable only accept whitelisted values from the rules config. Anything else leaves the cell empty and flags the row.
5. Two independent sources or a flag. Agreement across independent source families = green. Single source = yellow. Conflict or nothing found = red. ODM values are sanity-check hints, never primary sources (exceptions below: COO, price, qty).
6. Idempotent and cached. Per-GTIN JSON cache; re-runs fetch only misses; every run reproducible from cache.
7. Header-driven output. Templates vary; map through a header synonym table.

## 3. Pipeline

ingest -> resolve -> extract -> validate -> normalize -> categorize -> dangerous_goods -> emit

Per-field status enum: VERIFIED, SINGLE_SOURCE, CONFLICT, NOT_FOUND, MANUAL, ODM_SOURCED.

## 4. Repo layout

```
boozt-spec-builder/
  pyproject.toml
  src/bsb/
    cli.py                 # click commands: run, resolve, report
    models.py              # pydantic contracts
    ingest/odm.py          # ODM parser, header-block tolerant
    ingest/template.py     # header synonym mapping
    fetch/ladder.py        # httpx -> playwright -> firecrawl
    resolve/generic.py     # search + JSON-LD GTIN matcher
    resolve/adapters/nars.py
    extract/structured.py  # JSON-LD, og meta, SFCC payloads
    extract/llm.py         # schema-bound Anthropic fallback extractor
    validate/matrix.py
    normalize/boozt.py
    categorize/rules.py
    dg/msds.py             # SDS section 9/14 parser (Phase 1)
    emit/writer.py         # openpyxl output, colors, provenance sheet
  config/brands.yaml
  config/boozt_rules.yaml      # versioned to Boozt Guide v1.3
  config/header_synonyms.yaml
  cache/eans/{gtin13}.json
  tests/golden/                # the 3 example sheets as fixtures
```

## 5. Core data contracts

```python
class SourceRef(BaseModel):
    url: str
    method: Literal["jsonld", "sfcc_api", "dom", "llm_extract", "odm", "msds", "override"]  # "override" added 2026-07-03: per-order human decisions
    fetched_at: datetime
    snippet: str = ""

class FieldValue(BaseModel):
    value: str | None = None
    status: Literal["VERIFIED", "SINGLE_SOURCE", "CONFLICT",
                    "NOT_FOUND", "MANUAL", "ODM_SOURCED"] = "NOT_FOUND"
    primary: SourceRef | None = None
    secondary: SourceRef | None = None
    notes: str = ""

class ProductRecord(BaseModel):
    ean12: str                 # as in ODM and final sheet
    gtin13: str                # "0" + ean12, used for site lookups
    brand: str
    style_name: FieldValue
    color_name: FieldValue
    size: FieldValue
    ingredients: FieldValue
    gender: FieldValue
    category: FieldValue
    color_code: FieldValue
    flammable: FieldValue
    style_number: FieldValue
    country_iso: FieldValue
    dg: dict | None = None     # DG block, Phase 1
    odm_hints: dict = {}       # name, size, unit, coo, qty, price, subcategory
```

## 6. Stage specifications

### 6.1 Ingest (ODM)

Locate the table header row by finding the row containing at least {"Barcode", "Name", "QTY"} (in OR26BZQN0001 it is row 7; a metadata block sits above). Read barcodes as text, never as numbers (leading-zero and precision safety). Extract hints: Name, Size, Size Unit, COO, Gender, Client Price, QTY, Subcategory.

Checks at ingest: GS1 mod-10 check digit on every barcode (all 119 pass in this order); duplicate detection; length profile (this order: all 12-digit UPC).

GTIN forms: keep ean12 for the output sheet (Boozt: an EAN-13 must not start with 0, so 12-digit UPCs are submitted as-is, matching her finished sheets) and gtin13 = "0" + ean12 for brand-site lookups (NARS item numbers use the 13-digit form).

### 6.2 Template mapping

Read row 1 of the uploaded template and map headers through config/header_synonyms.yaml. Seed table (canonical -> observed):

| canonical | observed header text |
|---|---|
| ean | EAN Code |
| style_name | Style/Display name |
| style_number | Style number |
| brand | Brand |
| color_name | Color Name |
| size | Size |
| length | Length (enter "No Length" if not applicable) |
| variation | Variation (enter "No Variant" if not applicable) |
| gender | Gender (F = Female, M = Male, U = Unisex) |
| category | Boozt Product Category |
| color_code | Boozt Color code |
| ingredients | Material composition |
| flammable | Flammable (Yes/No) |
| country_iso | Country ISO code |
| customs_se | Customs code/Tariff Code (Sweden) |
| un_code | UN code |
| psn | Proper Shipping Name |
| hazard_class | Hazard Class |
| packing_group | Packing Group |
| ems | EmS code |
| tunnel | Tunnel Restriction Code |
| marine | Marine Pollutant |
| flash_point | Flash Point Transportation |
| expiry_on_pack | Expiry date on packaging |
| purchase_price | Original Purchase Price |

Match case-insensitively on a normalized form (collapse whitespace, strip parentheticals). Unknown headers: warn and leave untouched. Headers present in the synonym table but absent from the template: skip silently (the Olaplex layout has no color_code).

### 6.3 Resolve

Per brand, in order of preference:

1. Brand adapter. NARS runs Salesforce Commerce Cloud SFRA (live capture, 2026-07-02): there is NO browser-side OCAPI client_id — the OCAPI/.env path is dead, deleted from the plan. Confirmed architecture:
   - Controller base: https://www.narscosmetics.eu/on/demandware.store/Sites-nars_eu-Site/default/
   - Variation endpoint: Product-Variation?pid={masterId}&dwvar_{masterId}_color={colorValId}&Quantity=1&format=ajax. CORRECTION (gate B, 2026-07-02): the dwvar color value id is the variant's GTIN-13 on some masters (Powder Blush, Total Seduction Eyeshadow Stick) but an internal shade code on others (both foundation masters, e.g. '4251070360' for Oslo) — an unknown value silently returns the master's default variant. The adapter joins gtin13 -> shade name -> color val id from the master PDP's swatch data, and the returned partial's product-state "ID" must equal the requested gtin13 or the payload is rejected (GTIN-anchor rule — this is what caught the mismatch). Record each call's full URL as provenance.
   - PDPs live at /en/{slug}/{gtin13}.html, addressed by variant GTIN. Master example: 999NAC0000192 (Powder Blush) with variants 0194251140407 / 0194251140414.
   Adapter strategy, try in order:
   a. Plain httpx GET of one variant PDP per base-name group to discover the master pid and full swatch list, then cookie-less httpx Product-Variation per EAN; assert the returned variant id equals the requested gtin13, else reject.
   b. If a response is a bot-shell or the controller requires a session: Playwright, accept the consent banner once, keep the context alive, and route calls through the context's APIRequestContext so they inherit real runtime cookies/fingerprint. Never hardcode captured cookies — mint fresh sessions at runtime.
   c. Firecrawl API render as managed fallback if the site fights headless traffic.
   d. Archive fallback (added 2026-07-03): when a product is gone from the current brand site in every region (410/404/stateless), Wayback Machine snapshots of the brand's own PDP for that GTIN are a valid fallback source — same GTIN-anchor rule, ships yellow (never green from archive alone), provenance records the snapshot date, note "delisted from current site; filled from archived brand page". web.archive.org gets the standard politeness rules. Current site stays the preferred evidence; current-vs-historical conflict handling is unchanged, with one narrow exception: same barcode, brand relaunch changed a physical spec, and which version we hold is genuinely unknown — a human decision flagged VERIFY_AT_RECEIPT (confirm against physical goods at warehouse receipt).
   Ignore Bazaarvoice, genki, and analytics endpoints entirely.
2. Generic resolver (any brand, and validators): web search for "{gtin13}" and "{ean12} {brand}", collect candidate URLs, fetch, parse JSON-LD/microdata with extruct, accept only documents whose gtin/gtin12/gtin13/ean equals the item. Extract Product.name, color/variant, size, and ingredient fields when present.
3. Validator pool (independent from the brand family): Boots, Sephora, Douglas, Flaconi, Lookfantastic for name/shade/size; INCIDecoder, Boots, SkinSafe for INCI. Same GTIN-anchor rule. A page without a GTIN assertion may only serve as WEAK support via exact brand+product+shade string match, and can never turn a field green on its own. narscosmetics.eu, .co.uk and .com count as ONE source family, not mutual validators.

Shade-family efficiency: group ODM rows by base name (split on " - ", 116 of 119 rows here; 27 distinct base products, e.g. Natural Radiant Longwear Foundation x16, Light Reflecting Foundation x15, Pure Radiant Tinted Moisturizer x10). Resolve each master once to obtain the authoritative shade list, then confirm each EAN's shade via its own gtin13 PDP or the variation payload. 27 master lookups plus 119 cheap confirmations instead of 119 independent hunts.

Fetcher ladder and politeness: httpx with honest UA -> Playwright -> Firecrawl. Max 1 request per 2 seconds per host, exponential backoff, per-host stop-loss, cache everything. Prefer structured endpoints over scraping wherever they exist.

### 6.4 Extract

Structured-first. Every extracted field records its JSON path or CSS selector plus a snippet. LLM fallback (Anthropic API, temperature 0) only runs on already-fetched content with this contract:

```json
{
  "style_name": "...", "shade": "...",
  "size_value": "...", "size_unit": "ml|g|pcs|null",
  "inci": "... | null",
  "evidence": {"style_name": "verbatim substring", "shade": "...", "...": "..."}
}
```

Code verifies each evidence string is a substring of the fetched text; any failure rejects the whole extraction. The LLM never sees the ODM hints during extraction (no answer leakage).

### 6.5 Validate

Field matrix. Normalize before comparing (case, whitespace, decimal comma to dot, unit aliases gr/g, ML/ml). Rules:

- Agreement across two independent families -> VERIFIED (green).
- One family only -> SINGLE_SOURCE (yellow).
- Disagreement -> CONFLICT (red) with both values and URLs in notes.
- INCI: token-sequence compare. Identical -> green. Differences confined to the "May Contain/+/-" block -> yellow with a rendered diff (shade-specific colorants are the known error surface). Base-list differences -> red.
- ODM hints as tertiary checks: fuzzy name+shade similarity below threshold adds a note; scraped size disagreeing with ODM Size+Unit (e.g. 4.4 GR) downgrades to yellow.

### 6.6 Normalize (Boozt Guide v1.3 rules, encoded in config/boozt_rules.yaml)

- style_name: master product name without shade or size, English, max 60 chars, no abbreviations. One name per style number.
- color_name: the brand's exact shade string, verbatim, trimmed. No translation, no paraphrase.
- size: "{value} {unit}" with dot decimals; unit in {ml, g, pcs}; sets, tools and similar -> "One Size". "4,4gr" normalizes to "4.4 g".
- ingredients: INCI names, comma plus space separated, source order preserved, no "Ingredients:" prefix, no marketing prose. Keep the May Contain block verbatim as published for that shade.
- style_number: per-brand pattern from config, default "{prefix}{ean12}" (finished sheets show SVR+EAN and AEOAES+EAN; Olaplex deviates). NARS prefix is an open question; until confirmed the column stays MANUAL.
- gender: per-brand default from config (NARS: F, per her finished work; SVR and Olaplex: U). The ODM's blanket "unisex" is ignored as unreliable.
- country_iso: taken from ODM COO (status ODM_SOURCED); note added if brand data disagrees. This order: CA, US, IT, KR.
- flammable: "No" default for non-DG categories; DG logic below.
- Global whitespace lint: strip non-breaking spaces and leading/trailing whitespace on every cell (regression cases 2 and 4).

### 6.7 Categorize (fail closed)

Product Category decision rules keyed on product type detected from the brand's own taxonomy and product name, not from the ODM subcategory:

- foundation, concealer, BB cream, CC cream, tinted moisturizer -> "Foundation". This is the single biggest error surface in OR26BZQN0001: roughly 67 of 119 rows (foundations 31, concealers 26, tinted moisturizers 10) despite the ODM calling them "Face Make-Up".
- other color cosmetics (blush, bronzer, powders, primer, mascara, eyeshadow, brow, lip color) -> "Makeup".
- brushes, applicators -> "Makeup tools"; sets and kits -> "Sets"; fragrance with alcohol -> "Perfumes"; nail polish and removers -> "Nail polish"; face skincare -> "Skin care"; body -> "Body Care"; the full enum from Guide section 1.1.19 lives in boozt_rules.yaml.

Boozt Color code (enum 1001 to 1022):

1. Skincare and colorless products -> 1017 Clear.
2. Foundations and concealers -> 1018 Natural (guide: 1018 is used only for these). Deterministically covers the ~67 Foundation rows. Confirm once with her.
3. Everything else, in order: (a) brand swatch hex from the adapter payload mapped to the nearest of the 22 anchors; (b) curated shade lexicon (Orgasm -> 1003 Pink, Laguna -> 1010 Brown, Deep Rose -> 1003, etc., grown per run); (c) LLM proposal constrained to the enum with a one-line rationale, always yellow.

Anything the rules cannot decide stays empty and red. The tool never invents a category.

### 6.8 Dangerous goods module (Phase 1; OR26BZQN0001 contains no DG categories, so this does not block the urgent order)

Trigger: category in {Perfumes, Perfume set, Nail polish, Deodorants, Home & Spa sprays} or INCI beginning with Alcohol / Alcohol Denat. Input: the supplier MSDS/SDS PDF per item. Parse Section 14 (UN number, proper shipping name, hazard class, packing group, EmS, tunnel code, marine pollutant) and Section 9 (flash point). Cross-validate packing group against flash point per the guide table (PG2 < 23C; PG3 23 to 61C). The Aesop sheet anchors the expected output shape (UN 1170, Ethanol, class 3, PG 2, EmS F-E,S-D, flash 21, tunnel (D/E), marine Yes, flammable Yes). DG rows are always red until a human confirms; the tool prefills values and cites the SDS page number.

### 6.9 Emit

Write into a copy of the uploaded template, header-mapped. EAN column formatted as text. Fill colors: green VERIFIED, yellow SINGLE_SOURCE or attention, red CONFLICT, NOT_FOUND or DG. Two extra sheets: "Provenance" (ean, field, value, status, primary URL, secondary URL, method, snippet) and "Run report" (totals per status, review queue sorted red then yellow). Console summary mirrors the run report.

## 7. CLI

```
bsb run --odm ODM.xlsx --template TEMPLATE.xlsx --brand nars --out OUT.xlsx
bsb resolve --gtin 0194251147000 --brand nars -v     # single-item debug
bsb report --run cache/runs/2026-07-02T.../
```

## 8. Testing

- Golden tests: replay the three finished sheets from recorded fixtures and diff the auto-fillable fields.
- Lint regressions: all five observed manual errors (section 1) must be flagged.
- Guide validations: category enum, color enum, size regex, 60-char name limit, GS1 check digit, EAN-13 leading-zero rule, PG vs flash point table.
- Adapter contract tests run against recorded HTML/JSON fixtures; no network in CI.

## 9. Run plan for OR26BZQN0001

1. Scaffold the repo in Claude Code; implement ingest, normalize, categorize, emit first. These are pure-local and fully testable against the provided files before any network code exists.
2. NARS adapter (SFCC API if client_id captured, else Playwright). Resolve the 27 masters, then the 119 variants. Cache all payloads.
3. Validator pass: retailer JSON-LD for name, shade, size; INCIDecoder or Boots for INCI.
4. Emit. She reviews only yellows and reds. Realistic target: 100+ rows green, manual review under an hour instead of two days.

## 10. Form factor and phasing

- Phase 0 (now): CLI run by Oli via Claude Code. The pipeline library is the actual product; a web front end first would spend the urgent week on plumbing while all project risk sits in acquisition and validation.
- Phase 1: hardening. DG/MSDS module, adapters for recurring brands (Aesop, Olaplex, SVR), golden test suite, shade lexicon growth.
- Phase 2 (self-serve): thin web app on the NoSheet stack. Supabase stores runs, provenance and rules versions; flow: upload ODM + template -> job -> review UI for flagged fields -> export. The Python pipeline stays untouched underneath. Interim alternative: a Claude Code slash command Oli runs on request.

## 11. Config sketches

```yaml
# brands.yaml
nars:
  adapter: nars_sfcc
  domains: [narscosmetics.eu]
  gtin_form: "13"
  style_prefix: null        # OPEN QUESTION
  gender_default: F
  country_iso: from_odm

# boozt_rules.yaml (excerpt)
guide_version: "1.3"
name_max_chars: 60
size_pattern: '^\d+(\.\d+)? (ml|g|pcs)$|^One Size$'
color_codes: {1001: Cream, 1002: Beige, 1003: Pink, ..., 1022: Burgundy}
category_overrides:
  foundation_family: [foundation, concealer, bb cream, cc cream, tinted moisturizer]
```

Per-order manual decisions live in config/order_overrides/{ORDER}.yaml (field, eans, value, status, decided_by, date, rationale); they replace pipeline values at the end of the run and appear in the Provenance sheet as the deciding source (method "override").

## 12. Open questions

1. NARS style-number prefix for column C (finished sheets show prefix+EAN; the WIP row contains an SVR paste error, so the intended NARS convention is unconfirmed).
2. Confirm foundations and concealers always take color code 1018 Natural.
3. Which no-color convention to standardize on: CLEAR vs NO COLOR vs NO SHADE.
4. Are supplier MSDS PDFs available per item for DG orders (needed for Phase 1)?
5. Firecrawl account, or start Playwright-only? (Playwright-only is fine to start.)
6. Gender defaults per brand beyond NARS=F.

## 13. Ops notes

Secrets via .env, never committed: ANTHROPIC_API_KEY, optional FIRECRAWL_API_KEY. (The SFCC client_id entry is obsolete: narscosmetics.eu is SFRA with no browser-side client_id.) Politeness defaults always on (rate limit, backoff, cache-first). Each run writes to a timestamped folder with an input hash for reproducibility. The tool's honest guarantee is not "100% correct" but "100% of shipped values are source-verified or explicitly flagged for review"; nothing is ever silently guessed.
