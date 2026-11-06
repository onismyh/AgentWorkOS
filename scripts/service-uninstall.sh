#!/usr/bin/env bash
# Remove the AgentWorkOS launchd LaunchAgent (macOS) and stop the daemon. Data in var/ is kept.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.agentworkos.daemon"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "unloaded $LABEL" || echo "$LABEL was not loaded"
rm -f "$PLIST" && echo "removed $PLIST"
"$ROOT/scripts/awos" stop 2>/dev/null || true
