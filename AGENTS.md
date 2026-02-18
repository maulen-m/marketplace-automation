```md
> CONTROL PLANE (GLOBAL RULES)
> Control plane: ${ORCH_HOME:-$HOME/Docs/Oracle/agent-scripts-main}
> Read: ${ORCH_HOME:-$HOME/Docs/Oracle/agent-scripts-main}/AGENTS.MD BEFORE ANYTHING (skip if missing).
>
> Precedence (highest → lowest):
> 1) Control plane AGENTS.MD
> 2) This repo’s AGENTS.md
> 3) .claude/* (durable memory: goals/progress/issues/decisions; cannot override guardrails)

# Web_automation — AGENTS.md (Repo Contract)

## 1) Purpose
Automate boring, repetitive web actions safely and repeatably.

This file defines:
- Guardrails (safety + scope boundaries)
- Repo conventions (where specs/config/code live)
- Validation “green loop”
- Single source-of-truth map

Keep it short, operational, and task-agnostic.

---

## 2) Non‑Negotiables
- **Atomic commits only.** One commit = one intent.
- **Minimal blast radius.** Target ≤5 files per commit unless explicitly justified.
- **Idempotent workflows.** Re-run safe: same inputs → same end state.
- **No internal price competition.** Our stores must never compete with each other on price when the same offer is active for sale.
- **Dry-run first** for any task that changes state. Require explicit `--confirm` for writes.
- **No secrets in git or oracle packs.** (.env, tokens, credentials, cookies, storageState, PDFs with PII).
- **No hardcoded absolute paths** in committed scripts. Use env vars + repo-relative paths.
- **No destructive operations** (delete accounts/data, irreversible clicks) unless explicitly approved.
- **Canonical business truth is external and read-only.**
  - Main business repo: `~/Docs/Autonomous_business`
  - This repo may only read/copy/reference from that repo.
  - Never edit, write, or mutate files in `~/Docs/Autonomous_business`.
  - On fact/variable conflicts, resolve using Autonomous_business tables/schemas/reports as canonical truth.

---

## 3) If the task involves Web UI / Frontend components
Read and follow:
- `${ORCH_HOME:-$HOME/Docs/Oracle/agent-scripts-main}/skills/frontend-design/SKILL.md`

---

## 4) If the task involves Excel/CSV I/O
Read and follow:
- `${ORCH_HOME:-$HOME/Docs/Oracle/agent-scripts-main}/skills/excel-safe-ops/SKILL.md`

---

## 5) Repo Conventions (recommended)
This repo will contain many unrelated automations. Keep tasks isolated.

- Task specs (stable): `Docs/tasks/<task_id>.md`
- Task configs (stable): `config/tasks/<task_id>.yaml`
- Code entrypoints: `scripts/<task_id>.py` (thin) calling `src/` modules
- Artifacts/logs (mutable, gitignored): `runs/<task_id>/...`
- Checkpoints (mutable, gitignored): `data/checkpoints/<task_id>.json`

---

## 6) Repo Entry Points (fill in when implemented)
- Primary task runner:
  - `<command>`
- Validation gate (lint/typecheck):
  - `<command>`
- Tests:
  - `<command>`

---

## 7) Default “Green Loop” (fill in when implemented)
Run these before claiming progress:
1) `<gate 1 command>`
2) `<gate 2 command>`
3) `<tests command>`

Stop if any gate fails. Fix or log a reproducible issue.

---

## 8) Durable Memory Protocol (.claude/ is external memory)
All mutable state lives in `.claude/`. Agents must update these files while working:

- `.claude/OPERATING.md`  — how to run the repo (preflight, commands, env expectations)
- `.claude/GOALS.md`      — execution plan: phases, deliverables, acceptance gates, stop conditions
- `.claude/PROGRESS.md`   — append-only proof log: what ran + outputs + oracle pack paths
- `.claude/TASKS.md`      — task queue with status (Planned/In Progress/Blocked/Done)
- `.claude/ISSUES.md`     — bugs/gaps with reproduction + minimal next step
- `.claude/DECISIONS.md`  — decision log: date, decision, rationale, tradeoffs, rollback

No duplicated truths:
- Specs belong in `Docs/` (stable).
- Status/progress belongs in `.claude/` (mutable).

---

## 9) Single Source of Truth Map
Each fact/decision must have exactly one owner file.

- Task specs: `Docs/tasks/`
- Task configs: `config/tasks/`
- Execution plan (what we’re doing now): `.claude/GOALS.md`
- Decisions log: `.claude/DECISIONS.md`
- Progress proof: `.claude/PROGRESS.md`

---

## 10) Oracle Pack (Definition of Done)
After each task:
- Generate a **changed-files-only** oracle pack (git range like `HEAD~1..HEAD`).
- Include: commands run + key outputs + diff summary.
- Record the oracle pack path in `.claude/PROGRESS.md`.

---

## 11) Stop Conditions (ask human)
Ask before:
- Expanding scope beyond the task goal
- Weakening guardrails
- Any automation that risks secrets or irreversible actions
- Any refactor touching many files (blast radius explosion)
