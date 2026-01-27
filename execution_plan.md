# Execution Plan — Repricer Competitor Exclusions

Date: 2026-01-13
Owner: Codex

## Goal
Automate competitor exclusion selection in Repricer so our own stores do not compete against each other, using a safe, resumable Playwright workflow.

## Scope
- Implement a CLI-driven Playwright automation (Python)
- Config-driven accounts/stores/targets; no secrets in repo
- Headful by default; supports Chrome profile or storage_state.json
- Generate `storage_state.json` for authenticated sessions

## Non‑Goals
- Any backend/API integration
- Cross‑task framework beyond this automation

## Inputs
- Config file: `config/tasks/repricer_competitors.yaml`
- Env vars: `REPRICER_TOKEN_*`
- Chrome profile (for auth): `~/Library/Application Support/Google/Chrome/Profile 7`
- Store: `30000002` (initial validation)

## Phases & Steps

### Phase 1 — Repo scaffolding
1. Create Python package structure (`web_auto/`)
2. Add `requirements.txt`
3. Add CLI entrypoint (`./web-auto`) for local use

### Phase 2 — Core automation
1. Implement config loader + validation
2. Implement Playwright session management
   - Headful default
   - Support `--profile-dir` and `--storage-state`
3. Implement login modal close flow (`#alert_close`)
4. Implement table load + pagination
5. Implement competitor modal flow + checkbox idempotence
6. Implement checkpoint + resume logic

### Phase 3 — Auth persistence
1. Implement `web-auto auth` to generate `storage_state.json`
2. Store output in `data/storage_state.json` (not committed)

### Phase 4 — Run + verify
1. Run dry-run for store `30000002`
2. Run confirmed mode and spot-check persistence
3. Re-run to confirm idempotence (~0 changes)

## Deliverables
- `web_auto/` source package
- `web-auto` CLI script
- `requirements.txt`
- `config/tasks/repricer_competitors.yaml` (sample, no secrets)
- Generated `data/storage_state.json`
- Updated docs and `.claude` state

## Risks & Mitigations
- Login modal blocking UI → close `#alert_close`, remove backdrops
- Slow data load → wait for `#incoming_data_table_processing` to hide
- UI changes → selector map stored in `Docs/repricer_competitor_research_v2.md`

## Acceptance Gates
- Dry-run completes without unhandled errors
- Confirmed run persists after refresh
- Re-run results in ~0 changes

## Operational Workflow (Current)
1. Generate storage state:
   - `web-auto auth --base-url https://repricer.kz/price_strategy/ --token-env REPRICER_TOKEN_2 --profile-dir "/Users/<you>/Library/Application Support/Google/Chrome/Profile 7" --out data/storage_state.json --headed`
2. Confirm run (headful):
   - `web-auto run repricer-competitors --config config/tasks/repricer_competitors.yaml --storage-state data/storage_state.json --account app_2 --store 30000002 --confirm --headed`
3. Review + upload (only when remaining = 0):
   - `web-auto run repricer-competitors --config config/tasks/repricer_competitors.yaml --storage-state data/storage_state.json --account app_2 --store 30000002 --confirm --upload-after --headed`

## Status (2026-01-14)
- Automation implemented and verified on store `30000002`
- Upload clicked only after review confirmed 0 remaining targets
