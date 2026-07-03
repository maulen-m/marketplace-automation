#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PLIST_TEMPLATE="$REPO_ROOT/scripts/schedule/com.webautomation.suit21_ls_6990_suit31_ts_7990_price_ladder.plist.template"
PLIST_TARGET="$HOME/Library/LaunchAgents/com.webautomation.suit21_ls_6990_suit31_ts_7990_price_ladder.plist"
ENV_FILE="${WEB_AUTOMATION_ENV_FILE:-$HOME/Docs/Autonomous_business/.env}"
LABEL="com.webautomation.suit21_ls_6990_suit31_ts_7990_price_ladder"

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$REPO_ROOT/runs/scheduled_checkpoints/suit21_ls_6990_suit31_ts_7990_price_ladder/logs"

if [[ ! -f "$PLIST_TEMPLATE" ]]; then
  echo "Missing template: $PLIST_TEMPLATE" >&2
  exit 2
fi

"$REPO_ROOT/.venv/bin/python" -m web_auto --json scheduled-checkpoint validate \
  --config "$REPO_ROOT/config/schedules/suit21_ls_6990_suit31_ts_7990_price_ladder.yaml"

sed \
  -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
  -e "s|__ENV_FILE__|$ENV_FILE|g" \
  "$PLIST_TEMPLATE" > "$PLIST_TARGET"

launchctl bootout "gui/$(id -u)" "$PLIST_TARGET" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_TARGET"
launchctl enable "gui/$(id -u)/$LABEL"

echo "Installed: $PLIST_TARGET"
echo "Env file: $ENV_FILE"
launchctl print "gui/$(id -u)/$LABEL" | head -n 20 || true
