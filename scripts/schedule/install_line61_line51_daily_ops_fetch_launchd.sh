#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LABEL="com.webautomation.line61_line51_daily_ops_fetch"
PLIST_TEMPLATE="$REPO_ROOT/scripts/schedule/${LABEL}.plist.template"
PLIST_TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"
ENV_FILE="${WEB_AUTOMATION_ENV_FILE:-$HOME/Docs/Autonomous_business/.env}"

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$REPO_ROOT/runs/line61_line51_daily_ops_fetch/logs"

if [[ ! -f "$PLIST_TEMPLATE" ]]; then
  echo "Missing template: $PLIST_TEMPLATE" >&2
  exit 2
fi

"$REPO_ROOT/.venv/bin/python" -m web_auto --help >/dev/null

sed \
  -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
  -e "s|__ENV_FILE__|$ENV_FILE|g" \
  "$PLIST_TEMPLATE" > "$PLIST_TARGET"

launchctl bootout "gui/$(id -u)" "$PLIST_TARGET" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_TARGET"
launchctl enable "gui/$(id -u)/$LABEL"

echo "Installed: $PLIST_TARGET"
echo "Env file: $ENV_FILE"
launchctl print "gui/$(id -u)/$LABEL" | head -n 30 || true
