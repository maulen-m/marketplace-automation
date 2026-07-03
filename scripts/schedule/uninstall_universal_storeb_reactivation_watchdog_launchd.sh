#!/usr/bin/env bash
set -euo pipefail

LABEL="com.webautomation.universal_storeb_reactivation_watchdog"
PLIST_TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"

launchctl bootout "gui/$(id -u)" "$PLIST_TARGET" >/dev/null 2>&1 || true
rm -f "$PLIST_TARGET"

echo "Uninstalled: $PLIST_TARGET"
