# marketplace-automation

Browser and API automation for running a marketplace seller account: pricelist
generation, offer upload, competitor-aware repricing, and the safety controls
that stop an automated price loop from destroying margin.

Targets [Kaspi.kz](https://kaspi.kz) merchant cabinet and a third-party
repricing SaaS. Playwright for the UI paths, direct API where one exists.
Sanitized public synthesis of a production ops repo.

---

## The problem with automated repricing

A repricer that only tracks the lowest competitor converges on zero. A repricer
that ignores competitors loses the buy box. The interesting work is in the
guardrails, and most of this repo is guardrails.

**Price floors** (`web_auto/kaspi_price_floors.py`,
`config/price_floor_article_map.yaml`) — a per-article floor derived from COGS
and marketplace commission. No automated path may write below it, ever.

**Out-of-stock demotion** — a zero-stock offer is pushed to a deliberately
uncompetitive price rather than deactivated, so the listing keeps its history
and reviews without taking orders it cannot fill.

**Competitor exclusion** (`web_auto/repricer_competitors.py`,
`web_auto/competition_rules.py`) — partner and own-account storefronts are kept
in ignore lists, because racing your own second store to the bottom is the
easiest way to lose money automatically.

**Dumping detection** (`web_auto/repricer_dumping.py`) — identifies competitors
pricing below any plausible cost, which the repricer must not chase.

**Unified truth** (`web_auto/repricer_unified_truth.py`) — the repricer's view
and the marketplace's view of the same offer disagree routinely. One reconciled
record decides.

## Layout

```
web_auto/                 49 modules
  auth.py                 session/storage-state handling
  kaspi_pricelist_*.py    download, safe-patch, upload, verify
  kaspi_offer_ui_upload*  Playwright existing-card offer onboarding
  kaspi_marketing_*.py    ad controls, bid autopilot, direct-API pipeline
  repricer_*.py           competitor scan, min-price sync, protection, export
  kaspi_price_floors.py   the floor that nothing may cross
  checkpoint.py           restorable state before any write batch
  offer_flow_registry.py  declarative offer-state machines
config/tasks/             one YAML per scheduled job
scripts/                  one-shot builders and validators
skills/                   an operational playbook for the offer control plane
```

## Safety model

Every write path follows the same shape:

1. **Read and snapshot** current live state (`kaspi_hourly_snapshot.py`).
2. **Compute** the intended change, offline.
3. **Checkpoint** — write the restorable state (`checkpoint.py`).
4. **Dry-run** — emit the diff; this is the default.
5. **Apply** only under an explicit flag.
6. **Verify by fresh readback** — re-fetch ACTIVE/ARCHIVE state and assert the
   result, rather than trusting the write's own response.

Step 6 exists because the merchant UI reports success on uploads that are
accepted but not processed. "Uploaded" is not "applied."

## Running it

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env

python3 -m web_auto pricelist download --store STORE-A
python3 -m web_auto pricelist patch --in current.xlsx --out upload.xlsx --dry-run
python3 -m web_auto repricer sync-min-price --dry-run
```

Nothing writes without `--apply`.

## What is not in this repo

Exported workbooks and parquet datasets, inventory snapshots, run logs,
authenticated browser storage state, credentials, and `.claude/` working memory
— all removed from the full history. Brand, store and SKU identifiers are
placeholders; the third-party repricing SaaS appears as `Repricer`.

## Provenance

32 commits with their original January–July 2026 author dates; development
branches consolidated into `main`.

## License

MIT — see [LICENSE](LICENSE).
