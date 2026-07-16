#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LABEL="com.webautomation.bid_autopilot"
PLIST_TEMPLATE="$REPO_ROOT/scripts/schedule/${LABEL}.plist.template"
PLIST_TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"
BID_AUTOPILOT_ENV_FILE="${BID_AUTOPILOT_ENV_FILE:-$REPO_ROOT/runs/bid_autopilot/runtime/bid_autopilot.env}"

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$REPO_ROOT/runs/bid_autopilot/logs"

if [[ ! -f "$PLIST_TEMPLATE" ]]; then
  echo "missing_template:$PLIST_TEMPLATE" >&2
  exit 2
fi
if [[ ! -f "$BID_AUTOPILOT_ENV_FILE" ]]; then
  echo "missing_bid_autopilot_env_file:$BID_AUTOPILOT_ENV_FILE" >&2
  exit 2
fi

PERMS="$(stat -f "%Lp" "$BID_AUTOPILOT_ENV_FILE" 2>/dev/null || stat -c "%a" "$BID_AUTOPILOT_ENV_FILE")"
if [[ "$PERMS" != "600" ]]; then
  echo "bid_autopilot_env_permissions_must_be_600:$BID_AUTOPILOT_ENV_FILE" >&2
  exit 2
fi

"$REPO_ROOT/.venv/bin/python" -m web_auto.kaspi_marketing_bid_autopilot --help >/dev/null

sed \
  -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
  -e "s|__BID_AUTOPILOT_ENV_FILE__|$BID_AUTOPILOT_ENV_FILE|g" \
  "$PLIST_TEMPLATE" > "$PLIST_TARGET"

plutil -lint "$PLIST_TARGET" >/dev/null

launchctl bootout "gui/$(id -u)" "$PLIST_TARGET" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_TARGET"
launchctl enable "gui/$(id -u)/$LABEL"

echo "installed:$PLIST_TARGET"
echo "env_file:$BID_AUTOPILOT_ENV_FILE"
launchctl print "gui/$(id -u)/$LABEL" | head -n 30 || true
