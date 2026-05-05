# Web_automation — Practical Repo Guide

This repo automates Kaspi/Repricer operational workflows for store pricing, offer identity mapping, stock-aware pricelist generation, and competitor-control safety loops.

Primary in-scope stores:
- `UNIVERSAL` (`30000001`)
- `STORE-B` (`30000002`)

## 1) What This Repo Does

Core jobs:
- Build upload-ready Kaspi pricelists from current stock + offer mappings.
- Keep live Repricer min/max/current prices aligned to business rules.
- Map Kaspi offers to internal `sku_key` truth with deterministic fallbacks.
- Maintain safety controls: OOS demotion pricing, competitor ignore lists, non-sell blacklists.
- Generate traceable outputs (`exports/`, `runs/`) with backups and verification.

## 2) Where Things Live

- Logic:
  - `inventory/`
  - `web_auto/`
- Task configs:
  - `config/tasks/`
- Stable business/ops docs:
  - `Docs/`
- Runtime outputs:
  - `exports/` (workbooks/csv artifacts)
  - `runs/` (run summaries and logs)
- Operator memory/tracking:
  - `.claude/`

## 3) Canonical Terms (Important)

- `sku_key`:
  - Internal canonical SKU identifier (single-truth SKU key).
- `sku_id_ksp`:
  - Merchant-side Kaspi SKU/article (`артикул`), often embedded in offer rows.
- `kaspi_sku`:
  - Additional Kaspi SKU field used as fallback identity key.
- `resolved_url`:
  - Normalized Kaspi offer URL, the strongest identity anchor.
  - Invariant: `1 URL -> 1 sku_key`.
- `kaspi_offer_name`:
  - Kaspi-system canonical offer name (idempotent).
- `kaspi_name_source`:
  - Merchant custom naming (non-idempotent); do not treat as canonical identity.
- `resolved_kaspi_offer_name`:
  - Resolved canonical Kaspi offer name used in workflows.
- `final_attached_size`:
  - Final operational size attached to an offer row (used for stock mapping/allocation).

## 4) Offer Identification Logic (How Mapping Works)

Deterministic priority:
1. `resolved_url` / `link` exact match
2. `sku_id_ksp` exact match
3. `kaspi_sku` exact match

If unresolved:
- Keep row unresolved (do not force risky edits).
- Use controlled backfill only when evidence is strong.

Key rule:
- URL is the strongest stable cross-store identifier and should dominate merges/backfills.

## 5) offers_book Workbook Model

Main workbook:
- `exports/offers_book.xlsx`

Critical sheets:
- `offers_merged`:
  - Offer-level rows across stores with identity columns (`resolved_url`, `resolved_sku_key`, names, store, status fields).
- `size_level`:
  - Size attachment layer keyed by `offer_row_index`.
  - Holds `final_attached_size` and size-rule provenance.

Operational use:
- `offers_merged` provides identity.
- `size_level.final_attached_size` provides size for stock lookup/allocation.

## 6) Size Logic (What "Final Size" Means)

High-level behavior:
- URL/text extraction first (URL preferred, offer-name fallback).
- Probability/consensus over historical signals.
- Tie-break: larger size wins for equal share.
- Kids ladder is capped to: `22,24,26,28,30,S`.
- Electronics (`ELS`) use `ONE_SIZE`.

Special overrides exist for defined families (documented in `Docs/kaspi_item_truth.md`).

## 7) Pricelist Build Logic (End-to-End)

Main script:
- `inventory/build_pricelist_snapshots_and_uploads.py`

Inputs:
- Source On/Off templates:
  - `Docs/price_lists/21.02.2026_09_19/...`
- Offers identity/size:
  - `exports/offers_book.xlsx`
- Warehouse snapshot:
  - `exports/stock_snapshots/...`
- Profit floor truth:
  - `Docs/inventory/Dim_sku_light_v7.md` as fallback SKU anchors
  - `exports/pricelist_snapshots/min_price_floor_35pct_by_sku_v6.csv`

Flow:
1. Load and merge store On/Off source sheets.
2. Attach offer identity (`sku_key`, URL, names) and `final_attached_size`.
3. Resolve stock by `(sku_key, final_attached_size)`.
4. Allocate stock between stores (business allocation rules).
5. Apply force-off blacklists/restrictions.
6. Compute/attach `min_price_input` and `max_price_input`:
  - in-stock `min` follows floor35 (`Min_price_35pct`) unless explicit override.
7. Write snapshot + upload outputs using strict template columns.
8. Enforce numeric cell format for numeric columns (Kaspi upload requirement).
9. Backup files and append edit log.

Outputs:
- `exports/pricelist_snapshots/universal_snapshot_<date>.xlsx`
- `exports/pricelist_snapshots/store-b_snapshot_<date>.xlsx`
- `exports/pricelist_snapshots/upload/*_upload_<date>.xlsx`

## 8) Live Repricer Redflag Logic

Main apply script:
- `inventory/apply_repricer_minmax_from_snapshots.py`

What it enforces:
- OOS safety locks where required (`14990/14990` behavior by rule scope).
- Min/Max target sync from snapshot rules.
- DLRO:
  - `Dynamic Live Raise Opportunities` (`live_price_raise_updates`).
  - Raise live price toward valid external competitor floor within target min/max bounds.

Companion competitor-ignore automation:
- `web-auto run repricer-competitors ... --api --api-verify --confirm`
- Ensures our own stores/target sellers are in ignore lists.

## 9) Current Key Pricing Controls (Summary)

Detailed truth is in:
- `Docs/inventory/repricer_live_price_rules.md`
- `Docs/kaspi_item_truth.md`

Notable active behavior:
- In-stock min is floor35-driven.
- Shared URLs (`UNIVERSAL` + `STORE-B`) use store-priority spread: `UNIVERSAL` leads by at least `1` KZT on min (implemented as raise-only on STORE-B min when feasible).
- Shirt-family max `4990` applies to explicit allowlist (+ Nike-shirt fallback), with exclusions.
- Kid suit families (`KID-31`, `KID_ROMBIK`) are pinned to in-stock max `14990`.
- Line52 lock list behavior is URL-scoped and store-scoped by rule.

## 10) Most Useful Commands

- Redflag/DLRO dry scan:
```bash
set -a; source .env; set +a
PYTHONPATH=. ./.venv/bin/python inventory/apply_repricer_minmax_from_snapshots.py \
  --snapshot-date 2026-03-02 --headless --dry-run --verify
```

- Redflag/DLRO live apply:
```bash
set -a; source .env; set +a
PYTHONPATH=. ./.venv/bin/python inventory/apply_repricer_minmax_from_snapshots.py \
  --snapshot-date 2026-03-02 --headless --confirm --verify
```

- Competitor-ignore verify/apply:
```bash
set -a; source .env; set +a
./web-auto --json run repricer-competitors \
  --config config/tasks/repricer_competitors.yaml \
  --account app_1 --stores 30000001,30000002 \
  --api --api-verify --confirm --headless
```

- Build snapshots + uploads:
```bash
./.venv/bin/python inventory/build_pricelist_snapshots_and_uploads.py --confirm ...
```

## 11) Single Source of Truth Docs

If you read only a few docs, read these in order:
1. `Docs/kaspi_item_truth.md`
2. `Docs/inventory/repricer_live_price_rules.md`
3. `.claude/OPERATING.md`
4. `AGENTS.md`

## 12) Practical Rule of Thumb

- Never change offer identity based on weak text similarity when URL evidence exists.
- Never relax min/max safety rules to chase sales blindly.
- Treat DLRO as continuous market drift handling, not a one-time cleanup.
