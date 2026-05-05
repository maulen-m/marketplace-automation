# Web_automation Repo Contract

## Purpose
Automate boring, repetitive web actions safely and repeatably.

## Scope
- In scope: repo-local automation code, configs, stable docs, and generated local artifacts under this repo.
- Out of scope: writing to `~/Docs/Autonomous_business`, committing secrets, or taking irreversible live actions without explicit approval.

## Non-Negotiables
- Atomic commits only. One commit = one intent.
- Minimal blast radius. Target `<=5` files per commit unless explicitly justified.
- Idempotent workflows. Re-run safe: same inputs -> same end state.
- No internal price competition. Our stores must never compete with each other on price when the same offer is active for sale.
- Dry-run first for any task that changes state. Require explicit `--confirm` for writes.
- No secrets in git or oracle packs. (`.env`, tokens, credentials, cookies, storageState, PDFs with PII).
- No hardcoded absolute paths in committed scripts. Use env vars plus repo-relative paths.
- No destructive operations (delete accounts/data, irreversible clicks) unless explicitly approved.

## Canonical Truth And Ownership
- Main business truth is `~/Docs/Autonomous_business`.
- This repo may only read, copy, or reference from that repo.
- Never edit, write, or mutate files in `~/Docs/Autonomous_business`.
- On fact or variable conflicts, `Autonomous_business` tables, schemas, and reports win.
- Stable specs and rule docs live in `Docs/`.
- Stable task configs live in `config/tasks/`.
- Stable operator runbook lives in `Docs/00_START_HERE.md`.
- Mutable execution state lives only in:
  - `.claude/GOALS.md`
  - `.claude/TASKS.md`
  - `.claude/ISSUES.md`
  - `.claude/DECISIONS.md`
  - `.claude/PROGRESS.md`
  - `.claude/INSIGHTS.md`

## Pricelist Safety
- Before any overwrite of upload or snapshot `.xlsx`, create a timestamped backup in `exports/pricelist_snapshots/backups/`.
- Before overwriting related `.csv` artifacts (meta/log), create a timestamped backup in the same backup directory.
- Every upload pricelist rewrite must append a timestamped row to `exports/pricelist_snapshots/pricelist_edit_log.csv` with store, edited file path, and backup file path.

## Required Skill Handoffs
- If the task involves Web UI or frontend components, read and follow:
  - `${ORCH_HOME:-$HOME/Docs/Oracle/agent-scripts-main}/skills/frontend-design/SKILL.md`
- If the task involves Excel or CSV I/O, read and follow:
  - `${ORCH_HOME:-$HOME/Docs/Oracle/agent-scripts-main}/skills/excel-safe-ops/SKILL.md`

## Validation And Closeout
- Run the repo-specific green loop from `Docs/00_START_HERE.md` before claiming progress.
- Stop if any gate fails. Fix it or log a reproducible issue.
- After each task, generate a changed-files-only oracle pack (git range such as `HEAD~1..HEAD`).
- Include commands run, key outputs, and diff summary in that pack.
- Record the oracle pack path in `.claude/PROGRESS.md`.

## Stop And Ask Human
- Expanding scope beyond the task goal.
- Weakening guardrails.
- Any automation that risks secrets or irreversible actions.
- Any refactor touching many files (blast radius explosion).
