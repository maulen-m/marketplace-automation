Gate: GREEN

# Kaspi Marketing fetch date-match fix — 2026-07-27

## Result

Implemented the bounded offline fix from the dispatch.

- Product and category XHRs are now matched by their URL paths without requiring
  `StartDate` / `EndDate` to equal the navigated target date.
- The matched query dates are exposed as
  `payloads["_meta"]["effective_products_range"]` and
  `payloads["_meta"]["effective_categories_range"]`.
- Raw campaign evidence now records `target_date`, `effective_date`, and both
  effective ranges.
- Campaign and product daily rows use the single-day effective product range,
  so a July 25 payload navigated from a July 26 URL is keyed as July 25.
- A multi-day effective product aggregate fails closed rather than being
  silently stamped with the navigated date.
- Same-date XHR behavior is unchanged.

No STOP condition was triggered: no login-flow, DirectAPI-writer, CLI-surface,
or schema change was needed.

## Files and lines

- `web_auto/kaspi_marketing.py:327-428`
  - path-based product/category response matching;
  - effective range parsing and `_meta` fields;
  - single-day effective ingestion-date resolver.
- `web_auto/kaspi_marketing.py:783-835`
  - raw evidence fields and effective-date use for campaign/product rows.
- `inventory/test_kaspi_marketing_logic.py:28-160`
  - fake page/response fixtures;
  - different-date capture, non-matching campaign rejection, effective-range
    metadata, and same-date compatibility coverage.

## Commands and verification

```text
./.venv/bin/python -m pytest -q inventory/test_kaspi_marketing_logic.py
..............                                                           [100%]
14 passed in 0.14s

./.venv/bin/python -m py_compile web_auto/kaspi_marketing.py inventory/test_kaspi_marketing_logic.py
exit 0

git diff --check -- web_auto/kaspi_marketing.py inventory/test_kaspi_marketing_logic.py
exit 0
```

The required touched-file pytest suite and compilation gate are GREEN.

Two broader diagnostic commands were also run:

- `./.venv/bin/python -m pytest -q` stopped during collection because a
  checkpoint test under
  `exports/validation/channel_stock_repair_20260718/checkpoint_prewrite/`
  duplicates the basename of the canonical inventory test.
- `./.venv/bin/python -m pytest -q inventory` completed with
  `1336 passed, 27 failed`; the failures are in unrelated, already-dirty LINE31
  scope, offer/pricelist, Repricer goal/runbook, offer-flow, Line52, and
  registry surfaces. Neither this source file nor its focused test failed.

Those unrelated repository failures were not modified under this dispatch.

## Boundaries

- Network calls: 0
- Live Kaspi fetches: 0
- External writes: 0
- Database/schema writes: 0
- Files changed for implementation/tests: 2
- Live two-store acceptance: pending orchestrator execution, as assigned

## Effort pin

Executor pin echoed from the dispatch: Codex `gpt-5.6-sol` at machine-default
`xhigh`.
