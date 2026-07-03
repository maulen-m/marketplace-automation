#!/usr/bin/env bash
set -euo pipefail

LABEL="com.webautomation.acmewear_universal_marketing_ops_fetch"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PLIST_TEMPLATE="$REPO_ROOT/scripts/schedule/${LABEL}.plist.template"
PLIST_TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"
ENV_FILE="${WEB_AUTOMATION_ENV_FILE:-$HOME/Docs/Autonomous_business/.env}"
RUNNER="$REPO_ROOT/scripts/schedule/run_acmewear_universal_marketing_ops_fetch.sh"
TIER_CONFIG="$REPO_ROOT/config/schedules/acmewear_universal_marketing_ops_tiers.yaml"
AGENT2_CLOSEOUT="$REPO_ROOT/runs/universal_marketing_ops_20260618/AGENT_2_COLLECTOR_CLOSEOUT.md"
LOG_DIR="$REPO_ROOT/runs/acmewear_universal_marketing_ops_fetch/logs"

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$LOG_DIR"

if [[ ! -f "$PLIST_TEMPLATE" ]]; then
  echo "Missing template: $PLIST_TEMPLATE" >&2
  exit 2
fi
if [[ ! -f "$TIER_CONFIG" ]]; then
  echo "Missing tier config: $TIER_CONFIG" >&2
  exit 2
fi
if [[ ! -x "$RUNNER" ]]; then
  echo "Refusing to install: missing executable collector runner: $RUNNER" >&2
  exit 3
fi
if [[ ! -f "$AGENT2_CLOSEOUT" ]]; then
  echo "Refusing to install: Agent 2 collector closeout is missing: $AGENT2_CLOSEOUT" >&2
  exit 4
fi
if ! grep -Eq '^Gate: GREEN$' "$AGENT2_CLOSEOUT"; then
  if [[ "${ACMEWEAR_UNIVERSAL_MARKETING_OPS_ACCEPTABLE_SMOKE:-0}" != "1" ]]; then
    echo "Refusing to install: Agent 2 is not GREEN; set ACMEWEAR_UNIVERSAL_MARKETING_OPS_ACCEPTABLE_SMOKE=1 only after reviewed acceptable smoke evidence" >&2
    exit 4
  fi
fi

"/usr/bin/python3" - "$PLIST_TEMPLATE" "$PLIST_TARGET" "$REPO_ROOT" "$ENV_FILE" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

template = Path(sys.argv[1])
target = Path(sys.argv[2])
repo_root = sys.argv[3]
env_file = sys.argv[4]

text = template.read_text(encoding="utf-8")
text = text.replace("__REPO_ROOT__", repo_root)
text = text.replace("__ENV_FILE__", env_file)
target.write_text(text, encoding="utf-8")
PY

plutil -lint "$PLIST_TARGET" >/dev/null

"/usr/bin/python3" - "$PLIST_TARGET" "$REPO_ROOT" <<'PY'
from __future__ import annotations

import plistlib
import sys
from pathlib import Path

plist_path = Path(sys.argv[1])
repo_root = Path(sys.argv[2]).resolve()
expected_label = "com.webautomation.acmewear_universal_marketing_ops_fetch"
expected_log_root = repo_root / "runs" / "acmewear_universal_marketing_ops_fetch" / "logs"

with plist_path.open("rb") as handle:
    plist = plistlib.load(handle)

if plist.get("Label") != expected_label:
    raise SystemExit("Refusing to install: unexpected LaunchAgent label")
if plist.get("StartInterval") != 21600:
    raise SystemExit("Refusing to install: StartInterval must be 21600 for the six-hour universal collector")

command_text = "\n".join(str(item) for item in plist.get("ProgramArguments") or [])
required_tokens = (
    "run_acmewear_universal_marketing_ops_fetch.sh",
    "acmewear_universal_marketing_ops_tiers.yaml",
)
for token in required_tokens:
    if token not in command_text:
        raise SystemExit(f"Refusing to install: rendered command missing {token}")

for token in ("--confirm", "--upload", "upload-after", "--price", "--stock", "--bid", "--budget", "--state", "--write", "BID", "budget"):
    if token in command_text:
        raise SystemExit(f"Refusing to install: rendered command contains forbidden write token {token}")

for key in ("StandardOutPath", "StandardErrorPath"):
    path = Path(str(plist.get(key) or "")).resolve()
    if expected_log_root not in (path, *path.parents):
        raise SystemExit(f"Refusing to install: {key} is not under the repo-local log root")
PY

launchctl bootout "gui/$(id -u)" "$PLIST_TARGET" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_TARGET"
launchctl enable "gui/$(id -u)/$LABEL"

echo "Installed: $PLIST_TARGET"
echo "Env file: $ENV_FILE"
echo "Logs: $LOG_DIR"
launchctl print "gui/$(id -u)/$LABEL" | head -n 30 || true
