#!/usr/bin/env bash
set -euo pipefail

PLIST_TARGET="$HOME/Library/LaunchAgents/com.webautomation.rombik_repair_first_selective_checkpoint.plist"
LABEL="com.webautomation.rombik_repair_first_selective_checkpoint"

launchctl bootout "gui/$(id -u)" "$PLIST_TARGET" >/dev/null 2>&1 || true
launchctl disable "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
rm -f "$PLIST_TARGET"
echo "Uninstalled: $LABEL"
