#!/usr/bin/env bash
set -euo pipefail

LABEL="com.webautomation.acmewear_universal_marketing_ops_fetch"
PLIST_TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"

launchctl bootout "gui/$(id -u)" "$PLIST_TARGET" >/dev/null 2>&1 || true
rm -f "$PLIST_TARGET"

echo "Uninstalled: $LABEL"
