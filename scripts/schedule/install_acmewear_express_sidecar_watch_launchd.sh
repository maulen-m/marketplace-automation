#!/usr/bin/env bash
set -euo pipefail

LABEL="com.webautomation.acmewear_express_sidecar_watch"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PLIST_TEMPLATE="$REPO_ROOT/scripts/schedule/${LABEL}.plist.template"
PLIST_TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"
ENV_FILE="${WEB_AUTOMATION_ENV_FILE:-$HOME/Docs/Autonomous_business/.env}"
RUNNER="$REPO_ROOT/scripts/schedule/run_acmewear_express_sidecar_watch.sh"
LOG_DIR="$REPO_ROOT/runs/acmewear_express_selfpickup_sidecar/watch_schedule/logs"
LIVE_PROOF_CLOSEOUT="${ACMEWEAR_EXPRESS_WATCH_LIVE_PROOF_CLOSEOUT:-}"
DRY_RUN="${ACMEWEAR_EXPRESS_WATCH_INSTALL_DRY_RUN:-0}"
DRY_RUN_DIR="${ACMEWEAR_EXPRESS_WATCH_INSTALL_DRY_RUN_DIR:-$REPO_ROOT/runs/acmewear_express_selfpickup_sidecar/watch_install_dry_run/$(date +%Y%m%d_%H%M%S)}"
RENDERED_PLIST_TARGET="$PLIST_TARGET"
if [[ "$DRY_RUN" == "1" ]]; then
  RENDERED_PLIST_TARGET="$DRY_RUN_DIR/${LABEL}.plist"
fi

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
if [[ "$DRY_RUN" == "1" ]]; then
  mkdir -p "$DRY_RUN_DIR"
fi

if [[ ! -f "$PLIST_TEMPLATE" ]]; then
  echo "Missing template: $PLIST_TEMPLATE" >&2
  exit 2
fi
if [[ ! -x "$RUNNER" ]]; then
  echo "Refusing to install: missing executable watch runner: $RUNNER" >&2
  exit 3
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Refusing to install: env file missing: $ENV_FILE" >&2
  exit 4
fi
if [[ -z "$LIVE_PROOF_CLOSEOUT" || ! -f "$LIVE_PROOF_CLOSEOUT" ]]; then
  echo "Refusing to install: set ACMEWEAR_EXPRESS_WATCH_LIVE_PROOF_CLOSEOUT to a reviewed GREEN live fetch/watch closeout" >&2
  exit 5
fi
if ! grep -Eq '^Gate: GREEN$' "$LIVE_PROOF_CLOSEOUT"; then
  echo "Refusing to install: live proof closeout is not GREEN: $LIVE_PROOF_CLOSEOUT" >&2
  exit 5
fi

"/usr/bin/python3" - "$PLIST_TEMPLATE" "$RENDERED_PLIST_TARGET" "$REPO_ROOT" "$ENV_FILE" <<'PY'
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
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(text, encoding="utf-8")
PY

plutil -lint "$RENDERED_PLIST_TARGET" >/dev/null

"/usr/bin/python3" - "$RENDERED_PLIST_TARGET" "$REPO_ROOT" <<'PY'
from __future__ import annotations

import plistlib
import sys
from pathlib import Path

plist_path = Path(sys.argv[1])
repo_root = Path(sys.argv[2]).resolve()
expected_label = "com.webautomation.acmewear_express_sidecar_watch"
expected_log_root = repo_root / "runs" / "acmewear_express_selfpickup_sidecar" / "watch_schedule" / "logs"

with plist_path.open("rb") as handle:
    plist = plistlib.load(handle)

if plist.get("Label") != expected_label:
    raise SystemExit("Refusing to install: unexpected LaunchAgent label")
if plist.get("StartInterval") != 300:
    raise SystemExit("Refusing to install: StartInterval must be 300 for the Express watch")

command_text = "\n".join(str(item) for item in plist.get("ProgramArguments") or [])
required_tokens = ("run_acmewear_express_sidecar_watch.sh", "WEB_AUTOMATION_ENV_FILE=")
for token in required_tokens:
    if token not in command_text:
        raise SystemExit(f"Refusing to install: rendered command missing {token}")

for token in ("--confirm", "--upload", "upload-after", "--price", "--stock", "--bid", "--budget", "ASSEMBLE", "telegram-send", "print"):
    if token in command_text:
        raise SystemExit(f"Refusing to install: rendered command contains forbidden write token {token}")

for key in ("StandardOutPath", "StandardErrorPath"):
    path = Path(str(plist.get(key) or "")).resolve()
    if expected_log_root not in (path, *path.parents):
        raise SystemExit(f"Refusing to install: {key} is not under the repo-local log root")
PY

if [[ "$DRY_RUN" == "1" ]]; then
  cat > "$DRY_RUN_DIR/ACMEWEAR_EXPRESS_WATCH_INSTALL_DRY_RUN.md" <<EOF
# ACMEWEAR Express Sidecar Watch LaunchAgent Dry Run

Gate: GREEN

Status: dry-run rendered and validated only.

Rendered plist: $RENDERED_PLIST_TARGET

Would install to: $PLIST_TARGET

Env file: $ENV_FILE

Live proof closeout: $LIVE_PROOF_CLOSEOUT

Runner: $RUNNER

Boundary: no LaunchAgent was installed, no launchctl bootstrap/enable was called, no Kaspi API request, order mutation, Telegram send, print job, or Autonomous_business write was performed.
EOF
  echo "Dry-run validated: $RENDERED_PLIST_TARGET"
  echo "Dry-run report: $DRY_RUN_DIR/ACMEWEAR_EXPRESS_WATCH_INSTALL_DRY_RUN.md"
  exit 0
fi

launchctl bootout "gui/$(id -u)" "$PLIST_TARGET" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_TARGET"
launchctl enable "gui/$(id -u)/$LABEL"

echo "Installed: $PLIST_TARGET"
echo "Env file: $ENV_FILE"
echo "Live proof closeout: $LIVE_PROOF_CLOSEOUT"
echo "Logs: $LOG_DIR"
launchctl print "gui/$(id -u)/$LABEL" | head -n 30 || true
