#!/usr/bin/env bash
set -euo pipefail

LABEL="com.webautomation.universal_storeb_reactivation_watchdog"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PLIST_TEMPLATE="$REPO_ROOT/scripts/schedule/${LABEL}.plist.template"
PLIST_TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$REPO_ROOT/runs/universal_storeb_reactivation_watchdog/logs"

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$LOG_DIR"

if [[ ! -f "$PLIST_TEMPLATE" ]]; then
  echo "Missing template: $PLIST_TEMPLATE" >&2
  exit 2
fi

sed "s|__REPO_ROOT__|$REPO_ROOT|g" "$PLIST_TEMPLATE" > "$PLIST_TARGET"
plutil -lint "$PLIST_TARGET" >/dev/null

COMMAND_TEXT="$(
  /usr/bin/python3 - "$PLIST_TARGET" <<'PY'
import plistlib
import sys

with open(sys.argv[1], "rb") as handle:
    plist = plistlib.load(handle)
print("\n".join(str(item) for item in plist.get("ProgramArguments") or []))
PY
)"

if [[ "$COMMAND_TEXT" == *"ACMEWEAR"* ]]; then
  echo "Refusing to install: rendered command contains forbidden store token" >&2
  exit 3
fi
if [[ "$COMMAND_TEXT" != *"universal_storeb_reactivation_watchdog.yaml"* ]]; then
  echo "Refusing to install: rendered command does not reference the watchdog config" >&2
  exit 3
fi
if [[ "$COMMAND_TEXT" != *"--refresh-kaspi"* || "$COMMAND_TEXT" != *"--refresh-repricer"* || "$COMMAND_TEXT" != *"--headless"* ]]; then
  echo "Refusing to install: rendered command does not run the fresh headless watchdog cycle" >&2
  exit 3
fi

/usr/bin/python3 - "$PLIST_TARGET" "$REPO_ROOT" <<'PY'
import plistlib
import sys
from pathlib import Path

plist_path = Path(sys.argv[1])
repo_root = Path(sys.argv[2]).resolve()
expected_log_root = repo_root / "runs" / "universal_storeb_reactivation_watchdog" / "logs"

with plist_path.open("rb") as handle:
    plist = plistlib.load(handle)

if plist.get("Label") != "com.webautomation.universal_storeb_reactivation_watchdog":
    raise SystemExit("Refusing to install: unexpected label")

for key in ("StandardOutPath", "StandardErrorPath"):
    path = Path(str(plist.get(key) or "")).resolve()
    if expected_log_root not in (path, *path.parents):
        raise SystemExit(f"Refusing to install: {key} is not repo-local")
PY

launchctl bootout "gui/$(id -u)" "$PLIST_TARGET" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_TARGET"
launchctl enable "gui/$(id -u)/$LABEL"

echo "Installed: $PLIST_TARGET"
echo "Logs: $LOG_DIR"
launchctl print "gui/$(id -u)/$LABEL" | head -n 30 || true
