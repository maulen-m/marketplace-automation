#!/usr/bin/env bash
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

CONFIG_PATH="${LINE51_KO_CLEARANCE_CONFIG:-$REPO_ROOT/config/tasks/line51_ko_clearance_20260618.yaml}"
RUN_ROOT="${LINE51_KO_CLEARANCE_RUN_ROOT:-$REPO_ROOT/runs/line51_ko_clearance}"
TIMESTAMP="${LINE51_KO_CLEARANCE_TIMESTAMP:-$(date +%Y%m%d_%H%M%S)}"

cd "$REPO_ROOT" || exit 2

"$PYTHON_BIN" inventory/build_line51_ko_clearance_packet.py \
  --config "$CONFIG_PATH" \
  --run-root "$RUN_ROOT" \
  --timestamp "$TIMESTAMP" \
  --json
