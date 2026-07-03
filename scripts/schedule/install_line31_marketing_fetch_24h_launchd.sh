#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PLIST_TEMPLATE="$REPO_ROOT/scripts/schedule/com.webautomation.line31_marketing_fetch_24h.plist.template"
PLIST_TARGET="$HOME/Library/LaunchAgents/com.webautomation.line31_marketing_fetch_24h.plist"
ENV_FILE="${WEB_AUTOMATION_ENV_FILE:-$HOME/Docs/Autonomous_business/.env}"
END_AT="${LINE31_MARKETING_FETCH_24H_END_AT:-2026-05-27T17:45:00+05:00}"

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$REPO_ROOT/runs/scheduled_checkpoints/line31_marketing_fetch_24h/logs"

if [[ ! -f "$PLIST_TEMPLATE" ]]; then
  echo "Missing template: $PLIST_TEMPLATE" >&2
  exit 2
fi

sed \
  -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
  -e "s|__ENV_FILE__|$ENV_FILE|g" \
  -e "s|__END_AT__|$END_AT|g" \
  "$PLIST_TEMPLATE" > "$PLIST_TARGET"

launchctl bootout "gui/$(id -u)" "$PLIST_TARGET" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_TARGET"
launchctl enable "gui/$(id -u)/com.webautomation.line31_marketing_fetch_24h"

echo "Installed: $PLIST_TARGET"
echo "Env file: $ENV_FILE"
echo "End at: $END_AT"
launchctl print "gui/$(id -u)/com.webautomation.line31_marketing_fetch_24h" | head -n 30 || true
