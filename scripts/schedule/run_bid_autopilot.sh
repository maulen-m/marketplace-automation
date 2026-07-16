#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

ENV_FILE="${BID_AUTOPILOT_ENV_FILE:-$REPO_ROOT/runs/bid_autopilot/runtime/bid_autopilot.env}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "bid_autopilot_env_missing:$ENV_FILE" >&2
  exit 2
fi

PERMS="$(stat -f "%Lp" "$ENV_FILE" 2>/dev/null || stat -c "%a" "$ENV_FILE")"
if [[ "$PERMS" != "600" ]]; then
  echo "bid_autopilot_env_permissions_must_be_600:$ENV_FILE" >&2
  exit 2
fi

set -a
# shellcheck source=/dev/null
source "$ENV_FILE"
set +a

if [[ -z "${WEB_AUTO_BID_AUTOPILOT_STANDING_INSTRUMENT:-}" ]]; then
  echo "bid_autopilot_standing_instrument_env_missing" >&2
  exit 2
fi
if [[ ! -f "$WEB_AUTO_BID_AUTOPILOT_STANDING_INSTRUMENT" ]]; then
  echo "bid_autopilot_standing_instrument_file_missing" >&2
  exit 2
fi
if [[ -z "${WEB_AUTO_BID_AUTOPILOT_UNIT_CONTRIBUTION_KZT:-}" ]]; then
  echo "bid_autopilot_unit_contribution_env_missing" >&2
  exit 2
fi

SCHEDULE_MODE="${WEB_AUTO_BID_AUTOPILOT_SCHEDULE_MODE:-dry_run}"
case "$SCHEDULE_MODE" in
  dry_run)
    MODE_ARG="--dry-run"
    ;;
  confirm)
    MODE_ARG="--confirm"
    ;;
  *)
    echo "bid_autopilot_schedule_mode_invalid:$SCHEDULE_MODE" >&2
    exit 2
    ;;
esac

mkdir -p "$REPO_ROOT/runs/bid_autopilot/logs"
cd "$REPO_ROOT"

exec "$PYTHON_BIN" -m web_auto.kaspi_marketing_bid_autopilot \
  "$MODE_ARG" \
  --config "${WEB_AUTO_BID_AUTOPILOT_CONFIG:-config/tasks/bid_autopilot.yaml}" \
  --run-root "${WEB_AUTO_BID_AUTOPILOT_RUN_ROOT:-runs/bid_autopilot}" \
  --ledger-path "${WEB_AUTO_BID_AUTOPILOT_LEDGER_PATH:-runs/bid_autopilot/ledger.jsonl}" \
  "$@"
