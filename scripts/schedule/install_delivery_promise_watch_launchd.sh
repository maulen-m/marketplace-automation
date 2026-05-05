#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PLIST_TEMPLATE="$REPO_ROOT/scripts/schedule/com.webautomation.delivery_promise_watch.plist.template"
PLIST_TARGET="$HOME/Library/LaunchAgents/com.webautomation.delivery_promise_watch.plist"

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$REPO_ROOT/runs/delivery_promise_watch/logs"

if [[ ! -f "$PLIST_TEMPLATE" ]]; then
  echo "Missing template: $PLIST_TEMPLATE" >&2
  exit 2
fi

sed "s|__REPO_ROOT__|$REPO_ROOT|g" "$PLIST_TEMPLATE" > "$PLIST_TARGET"

launchctl bootout "gui/$(id -u)" "$PLIST_TARGET" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_TARGET"
launchctl enable "gui/$(id -u)/com.webautomation.delivery_promise_watch"

echo "Installed: $PLIST_TARGET"
launchctl print "gui/$(id -u)/com.webautomation.delivery_promise_watch" | head -n 20 || true
