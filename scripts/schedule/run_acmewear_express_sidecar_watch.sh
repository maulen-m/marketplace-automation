#!/usr/bin/env bash
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

ENV_FILE="${WEB_AUTOMATION_ENV_FILE:-$HOME/Docs/Autonomous_business/.env}"
TOKEN_ENV="${ACMEWEAR_EXPRESS_WATCH_TOKEN_ENV:-KASPI_TOKEN_ACMEWEAR}"
RUN_ROOT_REL="${ACMEWEAR_EXPRESS_WATCH_RUN_ROOT_REL:-runs/acmewear_express_selfpickup_sidecar/watch_schedule}"
RUN_ROOT="$REPO_ROOT/$RUN_ROOT_REL"
TIMESTAMP="${ACMEWEAR_EXPRESS_WATCH_TIMESTAMP:-$(date +%Y%m%d_%H%M%S)}"
RUN_DIR="$RUN_ROOT/$TIMESTAMP"
LOG_DIR="$RUN_DIR/logs"
SIDECAR_CSV_REL="${ACMEWEAR_EXPRESS_SIDECAR_CSV_REL:-data/acmewear_express_selfpickup_sidecar.csv}"
SIDECAR_CSV="$REPO_ROOT/$SIDECAR_CSV_REL"
LOOKBACK_HOURS="${ACMEWEAR_EXPRESS_WATCH_LOOKBACK_HOURS:-24}"
PAGE_SIZE="${ACMEWEAR_EXPRESS_WATCH_PAGE_SIZE:-100}"
MAX_PAGES="${ACMEWEAR_EXPRESS_WATCH_MAX_PAGES:-5}"
START_HOUR="${ACMEWEAR_EXPRESS_STAFFED_START_HOUR:-10}"
END_HOUR="${ACMEWEAR_EXPRESS_STAFFED_END_HOUR:-18}"
ENFORCE_STAFFED_HOURS="${ACMEWEAR_EXPRESS_ENFORCE_STAFFED_HOURS:-1}"
CURRENT_HOUR="${ACMEWEAR_EXPRESS_CURRENT_HOUR:-$(date +%H)}"

mkdir -p "$LOG_DIR" "$(dirname "$SIDECAR_CSV")"
cd "$REPO_ROOT" || exit 2

write_json() {
  local path="$1"
  shift
  "$PYTHON_BIN" - "$path" "$@" <<'PY'
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

path = Path(sys.argv[1])
status = sys.argv[2]
reason = sys.argv[3]
run_dir = sys.argv[4]
payload = {
    "schema_version": "acmewear_express_selfpickup_schedule_runner.v1",
    "status": status,
    "reason": reason,
    "generated_at": datetime.now(timezone(timedelta(hours=5))).isoformat(timespec="seconds"),
    "run_dir": run_dir,
    "external_writes": {
        "kaspi_order_mutation": False,
        "telegram_send": False,
        "print_job": False,
        "autonomous_business_write": False,
    },
}
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

if [[ "$ENFORCE_STAFFED_HOURS" == "1" ]]; then
  if (( 10#$CURRENT_HOUR < START_HOUR || 10#$CURRENT_HOUR >= END_HOUR )); then
    write_json "$RUN_DIR/runner_summary.json" "skipped" "outside_staffed_hours" "$RUN_DIR"
    write_json "$RUN_DIR/watch_heartbeat.json" "skipped" "outside_staffed_hours" "$RUN_DIR"
    cat > "$RUN_DIR/ACMEWEAR_EXPRESS_WATCH_RUN_CLOSEOUT.md" <<EOF
# ACMEWEAR Express Sidecar Watch Scheduled Run

Gate: GREEN

Status: skipped

Reason: outside staffed hours (${START_HOUR}:00-${END_HOUR}:00, Asia/Almaty).

No Kaspi API request, order mutation, Telegram send, print job, or Autonomous_business write was performed.
EOF
    exit 0
  fi
fi

STDOUT_PATH="$LOG_DIR/watch_stdout.json"
STDERR_PATH="$LOG_DIR/watch_stderr.txt"
COMMAND_PATH="$LOG_DIR/command.txt"

CMD=(
  "$PYTHON_BIN" -m web_auto
  --env-file "$ENV_FILE"
  --json
  acmewear-express-sidecar watch
  --token-env "$TOKEN_ENV"
  --run-dir "$RUN_DIR"
  --sidecar-csv "$SIDECAR_CSV"
  --update-sidecar
  --persist-actionable-only
  --cycles 1
  --lookback-hours "$LOOKBACK_HOURS"
  --page-size "$PAGE_SIZE"
  --max-pages "$MAX_PAGES"
)

printf '%q ' "${CMD[@]}" > "$COMMAND_PATH"
printf '\n' >> "$COMMAND_PATH"

"${CMD[@]}" > "$STDOUT_PATH" 2> "$STDERR_PATH"
RC=$?

if [[ "$RC" -eq 0 ]]; then
  GATE="GREEN"
  STATUS="success"
else
  GATE="YELLOW"
  STATUS="blocked"
fi

write_json "$RUN_DIR/runner_summary.json" "$STATUS" "watch_command_exit_${RC}" "$RUN_DIR"

cat > "$RUN_DIR/ACMEWEAR_EXPRESS_WATCH_RUN_CLOSEOUT.md" <<EOF
# ACMEWEAR Express Sidecar Watch Scheduled Run

Gate: $GATE

Status: $STATUS

Return code: $RC

Stdout: $STDOUT_PATH

Stderr: $STDERR_PATH

Command: $COMMAND_PATH

Sidecar CSV: $SIDECAR_CSV

Boundary: no Kaspi order mutation, ASSEMBLE, pickup completion, Telegram send, print job, or Autonomous_business write was performed by this scheduled runner.
EOF

exit "$RC"
