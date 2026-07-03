#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="${WEB_AUTOMATION_ENV_FILE:-$HOME/Docs/Autonomous_business/.env}"
END_AT="${LINE31_MARKETING_FETCH_24H_END_AT:-2026-05-27T17:45:00+05:00}"
PLIST_TARGET="$HOME/Library/LaunchAgents/com.webautomation.line31_marketing_fetch_24h.plist"
RUN_ROOT="$REPO_ROOT/runs/scheduled_checkpoints/line31_marketing_fetch_24h"
LOG_DIR="$RUN_ROOT/logs"
HEARTBEAT="$RUN_ROOT/launchd_heartbeat.jsonl"

mkdir -p "$LOG_DIR"

cd "$REPO_ROOT"

"$REPO_ROOT/.venv/bin/python" -m web_auto \
  --env-file "$ENV_FILE" \
  --json \
  scheduled-checkpoint run \
  --config config/schedules/line31_marketing_fetch_6h.yaml \
  --due \
  --mode live-readonly \
  --allow-stale \
  --max-jobs 1 \
  --run-root runs/scheduled_checkpoints/line31_marketing_fetch_24h

"$REPO_ROOT/.venv/bin/python" - <<'PY' "$END_AT" "$HEARTBEAT" "$PLIST_TARGET"
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

end_at = datetime.fromisoformat(sys.argv[1])
now = datetime.now(ZoneInfo("Asia/Almaty")).replace(microsecond=0)
heartbeat = Path(sys.argv[2])
plist_target = Path(sys.argv[3])
payload = {
    "checked_at": now.isoformat(),
    "end_at": end_at.isoformat(),
    "status": "active" if now < end_at else "expired_uninstalling",
    "plist_target": str(plist_target),
}
heartbeat.parent.mkdir(parents=True, exist_ok=True)
with heartbeat.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
if now >= end_at:
    subprocess.run(["launchctl", "bootout", f"gui/{subprocess.check_output(['id', '-u'], text=True).strip()}", str(plist_target)], check=False)
    try:
        plist_target.unlink()
    except FileNotFoundError:
        pass
PY
