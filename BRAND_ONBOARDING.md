# Brand Onboarding Playbook

Codified from the NARS rollout (2026-07-02/03). Reliability is rule #1:
every kit principle — GTIN anchoring, two independent source families for
green, enums fail closed, provenance on every cell — applies unchanged to
every brand. We expand coverage, not trust.

## 0. Prerequisites

- Brand exists in `config/brands.yaml` with `boozt_code`, `display_name`,
  `gender_default`, candidate `domains`. Out-of-scope brands (e.g. Soft
  Goat / apparel) carry `out_of_scope: true` and are never built.
- **Dangerous goods**: for fragrance/alcohol categories (Marc Jacobs, Aesop
  fragrances), the MSDS module (kit 6.8) is a prerequisite before ANY order
  is processed. It is currently a stub (`src/bsb/dg/msds.py`) — a separate
  workstream. Until it lands, DG orders are blocked, not best-efforted.

## 1. Probe (no order needed, no gates)

    bsb probe-brand <brand>

Detects the platform (Shopify / SFCC / unknown), tests EAN addressability
with real EANs (from finished sheets where we hold them, else the site's
own barcodes as a self-test), checks on-site INCI availability, and records
fixtures under `tests/fixtures/probes/<brand>/` plus a draft config entry.

Review the probe report before writing any config. Platform playbooks:

- **Shopify**: `adapter: shopify`, config-only. Native anchor: variant
  `barcode` == GTIN via products.json (paginated, cached), sitemap handles,
  `{handle}.js`. Mind multi-region domains (configure the market that
  matches the buy side).
- **SFCC/SFRA**: `adapter: sfcc`, config-only when the storefront matches
  the SFRA conventions (Product-Show?pid={gtin13} 301s to the PDP;
  product-state object; Product-Variation keyed by color val id — verify
  the val-id semantics per master, they vary WITHIN a brand).
- **Unknown / not EAN-addressable** (e.g. Marc Jacobs): `adapter: generic`.
  Retailer-primary policy applies — kit 6.3: primary evidence is
  generic-resolver retailer data, and GREEN requires TWO independent
  retailer families, never one. Requires FIRECRAWL_API_KEY for search.
- **No speculative bespoke adapters.** Anything beyond Shopify/SFCC/generic
  waits until a probe proves the need AND a real order exists.

If we hold a finished sheet for the brand (answer key), run the golden
comparison and record the agreement rate as reliability evidence before the
first order.

## 2. Production gates (only on a real order)

Every gate stops and shows output; anomalies (anchor rejections, shades
missing from swatch lists, size mismatches vs ODM hints, validator conflict
rate above ~10%) block progression until reviewed.

- **Gate A — one product**: `bsb resolve --gtin <one EAN> --brand <brand>`.
  Verify: anchoring evidence, parsed fields, provenance URLs by hand.
- **Gate B — pilot**: `bsb run --resolve --bases "<2-3 base names>"`.
  Verify per-master variant tables, validator agreement, INCI extraction.
  Expect per-brand quirks here (NARS gate B found the dwvar val-id
  semantics and delisted-shade PDPs).
- **Gate C — full order**: `bsb run --resolve` over the whole ODM +
  validator pass + emit. Review run-report totals and the queue by reason.
- **First-order elevated review**: Felina checks ALL flagged cells plus 10
  random GREEN cells against sources. A brand is "production" only after
  its first order passes this review.

## 3. Per-brand knowledge goes to config, not code

- Shade/name formatting: `shade_format`, `shade_format_overrides`,
  `name_format` (per product where needed — the Laguna rule).
- Curated shade lexicon: keyed per (brand, shade) in that brand's
  `shade_lexicon`. Multi-shade products (quad/palette/trio/duo markers)
  never resolve via the lexicon.
- Brand product-type category knowledge: `product_name_categories`.
- Defaults confirmed by Felina: `expiry_on_pack_default`,
  `style_number_policy`, gender.
- Per-order human decisions: `config/order_overrides/{ORDER}.yaml`
  (provenance method "override"); physical-spec doubts carry
  `verify_at_receipt: true` (warehouse receiving checklist).
- Validator pool assignment: `config/validators.yaml` (`brand_pools` —
  pharmacy brands get pharmacy retailers once any are reachable).

## 4. Category-specific columns

Hair/skin attribute columns (Hair type, Skin concerns, …) stay MANUAL this
phase. The header mapper passes unknown headers through untouched — that
behavior is load-bearing; never map them speculatively.


## 5. Per-brand readiness (2026-07-03) — "can we take a Boozt order for X tomorrow?"

Evidence: probes + goldens under `data/out/probe_*.json` / `golden_*.json`.
"Golden" = agreement vs Felina's finished sheets on sampled EANs. INCI
extraction runs only on GTIN-anchored pages; multi-language water naming is
equivalent at compare time only.

| Brand | Platform / path | Name | Size | Shade | INCI | First order still needs |
|---|---|---|---|---|---|---|
| **nars** | SFCC EU (+US, +archive) | ✅ verified | ✅ | ✅ lexicon+rules | ✅ brand PDPs | **production** — first order shipped clean |
| **olaplex** | Shopify (sitemap→handle.js) | ✅ w/ name_format config | ✅ golden 100% | n/a (no-color std) | 🟡 in descriptions, vintage diffs | order-EAN coverage check; gates A–C |
| **svr** | retailer-primary (generic) | 🟡 golden 27% — FR sources dominate, EN preferred when present | ✅ golden 100% | n/a (no-color std) | 🟡 ~20% coverage, quality varies by shop | INCI coverage lift (more families/EAN, pharmacy retailers); gates A–C |
| **aesop** | retailer-primary (generic) | 🟡 golden 57% (mechanical cleanups known) | ✅ golden 100% | n/a | 🟡 ~27% coverage | DG prerequisite for fragrance lines (MSDS parked); gates A–C |
| **aderma** | brand site via sitemap-EAN index (gtin on 3/3 PDPs, EAN in URL) | ✅ expected | ✅ expected | n/a likely | ✅ on PDPs | implement the sitemap-EAN index strategy (config-level); gates A–C |
| **k18** | Shopify (handle.js barcodes) | ✅ expected | ✅ expected | n/a likely | ⚠️ not on site — retailer INCI needed | INCI source; gates A–C |
| **colorescience** | Shopify (barcodes suppressed in products.json) | ✅ expected | ✅ expected | shades exist — axis check | ✅ on PDPs | handle.js barcode recheck with order EANs; gates A–C |
| **maria_nila** | Shopify (barcodes suppressed-ish) | ✅ expected | ✅ expected | shades exist — axis check | ✅ on PDPs | handle.js barcode recheck; gates A–C |
| **benefit** | SFCC, controllers hidden (homepage+PDP) | via retailers | via retailers | shades exist | unknown | live session capture of controller URLs, else retailer-primary; gates A–C |
| **marc_jacobs** | SFCC, controller base FOUND, untested (no EANs held) | unknown | unknown | n/a | unknown | **blocked on MSDS module** (fragrance = DG); test Product-Show with first ODM EANs |
| avene | retailer-primary (global site is a brochure; no product sitemap) | via retailers | via retailers | n/a | via retailers/pharmacy | locale-site probe (.fr) worth one retry; pharmacy validators unproven |
| soft_goat | — | — | — | — | — | out of scope (apparel) |

Cross-brand levers that raise every 🟡 at once: more anchored families per
EAN in the generic resolver (cost: ~1 search + ~1 scrape per extra family),
pharmacy retailer INCI (blocked on reachable pharmacy validators), and the
key-gated LLM INCI isolator (evidence-substring rule) for pages the
deterministic parser can't segment.
