---
name: kaspi-offer-ops-control-plane
description: Route Kaspi offer creation, upload, recovery, and post-publication work in Web_automation into the correct lane, with explicit read order, preflight gates, stoplines, evidence rules, and recovery branches.
---

# Kaspi Offer Ops Control Plane

Use this skill when the task is any of:
- create new Kaspi offers
- upload prepared offer packages
- attach merchant offers to existing cards
- repair or recover missing offers
- replay rejected TRASH offers
- capture post-publication truth for downstream storage
- choose the best method for a future offer rollout

This skill is the routing layer. It helps choose the right lane and enforce the same closeout pattern every time.

## Required Read Order
1. `AGENTS.md`
2. `Docs/00_START_HERE.md`
3. `config/offer_flows.yaml`
4. `Docs/offer_ops/00_START_HERE.md`
5. `Docs/offer_ops/OFFER_METHOD_MATRIX.md`
6. `Docs/offer_ops/OFFER_STATE_MACHINE.md`
7. `Docs/offer_ops/RECOVERY_PLAYBOOK.md`
8. lane-specific session docs or handoffs if they already exist

## Registry First
Before classifying a fresh task, prefer the executable registry:
```bash
python3 -m web_auto.cli offer-flow validate
python3 -m web_auto.cli offer-flow select --goal <goal> --artifact-state <state> --store <store>
python3 -m web_auto.cli offer-flow show <flow_id>
```

Use the docs cluster to understand the lane. Use the registry to choose the lane.

Once the lane is chosen, prefer staging it through the wrapper:
```bash
python3 -m web_auto.cli offer-run --flow <flow_id> ...
```

For supported lanes, prefer dispatching through the same wrapper instead of hand-reconstructing the native command:
```bash
python3 -m web_auto.cli offer-run --flow kaspi-pricelist-sync --store <STORE> --intent <INTENT> --dispatch --dry-run
```

## Lane Selection Rules

### 1. Merchant ZIP import lane
Choose this when:
- new catalog-offer ZIPs are already prepared
- upload order and family grouping are defined
- the task is merchant import, not workbook field editing

Do not use this lane when the real work is only stock or price changes on existing merchant rows.

### 2. Existing-card UI upload lane
Choose this when:
- the public Kaspi card already exists
- rows are normalized in an `offer upload` workbook
- success depends on URL search, card match, and size-chip selection

Primary runner:
```bash
python3 -m web_auto.kaspi_offer_ui_upload --list-windows --json
python3 -m web_auto.kaspi_offer_ui_upload --workbook <xlsx> --stores <stores> --dry-run --json
python3 -m web_auto.kaspi_offer_ui_upload --workbook <xlsx> --stores <stores> --window-map <map> --confirm --json
```

### 3. Pricelist lane
Choose this when:
- merchant SKU already exists
- change is limited to ACTIVE or ARCHIVE sellability, warehouse stock, or price

Primary runner:
```bash
python3 -m web_auto.cli kaspi-pricelist sync --store <STORE> --intent <INTENT> --headless
python3 -m web_auto.cli kaspi-pricelist sync --store <STORE> --intent <INTENT> --upload --verify-after-upload --headless
```

### 4. Rejected-offer replay lane
Choose this when:
- merchant import succeeded
- Kaspi moved rows into `#/products/pending/TRASH/1`
- clarification text or replay action is required

Primary runner:
```bash
python3 -m web_auto.kaspi_pending_trash_dispute --store ACMEWEAR --json
python3 -m web_auto.kaspi_pending_trash_dispute --store ACMEWEAR --confirm --json
```

### 5. Post-publication enrichment lane
Choose this when:
- cards are already live
- remaining work is URL capture, group relation capture, or downstream truth handoff

This repo records the memo and handoff. It does not directly mutate `Autonomous_business`.

## Always Classify Inputs First
Before using any lane, classify:
- target store and merchant ID
- current artifact type:
  - prepared ZIP queue
  - reviewed `offer upload` workbook
  - fresh ACTIVE and ARCHIVE downloads
  - current TRASH row set
  - live public URLs only
- whether the task is:
  - new-offer creation
  - existing-offer activation or recovery
  - stock or price maintenance
  - moderation replay
  - post-publication truth capture

## Preflight Contract
- Run read-only or dry-run first.
- Verify merchant identity before any UI write.
- Verify file presence and schema before upload.
- Keep one primary lane at a time.
- Stop if the state machine says to reclassify instead of retrying.

## Store-Specific Guardrails
- `UNIVERSAL` and `STORE-B`:
  after sellability changes, wait about 3 minutes, verify Repricer import, verify rows, verify dumping, then run redflag if the workflow requires it.
- `ACMEWEAR`:
  do not route fixed-price offer creation into Repricer unless the task explicitly asks for it.

## Stoplines
- upstream truth for a new-offer package is not green
- merchant session belongs to the wrong store
- current lane reports a documented branch condition:
  - new rows unrecognized in pricelist upload
  - TRASH clarification required after import
  - live page contradicts nominal UI upload success
- verification artifacts are missing or red

## Evidence Contract
Every completed run should leave:
- lane-specific `runs/...` artifacts
- `summary.json` or equivalent
- screenshots or logs when the lane uses browser automation
- a session memo when the rollout is new, cross-repo, or strategically important
- a `.claude/PROGRESS.md` append entry

## Recovery Contract
If a lane fails, read `Docs/offer_ops/RECOVERY_PLAYBOOK.md` before inventing a workaround.

The normal escalation order is:
1. lane-native retry or repair
2. documented branch to the next lane
3. manual UI only when the verified automation path is unavailable

## Related Skills
- Use `excel-safe-ops` whenever editing XLSX, XLSM, or CSV artifacts.
- Use `kaspi-pricelist-download-modify-upload` for pricelist workflows.
- Use `kaspi-offer-ui-upload-master` when the task is explicitly workbook-driven existing-card ingestion.
- Use `kaspi-offer-creation` for upstream package creation logic outside this repo's direct execution layer.
