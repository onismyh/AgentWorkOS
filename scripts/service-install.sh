#!/usr/bin/env bash
# Register AgentWorkOS as a per-user launchd LaunchAgent on macOS (counterpart of service-install.ps1).
# Idempotent: re-run to update.
#
#   ~/Library/LaunchAgents/com.agentworkos.daemon.plist
#     .venv/bin/python -X utf8 -m awos.cli daemon    (working dir = repo root)
#     RunAtLoad (starts at login and right now), KeepAlive on crash (launchd restarts it; ThrottleInterval 60 s),
#     a clean `awos stop` is not restarted. stdout/stderr -> var/logs/launchd.log (the daemon logs to var/logs/daemon.log).
#   launchd replaces the Windows watchdog task: it restarts the daemon itself.
#
# usage: scripts/service-install.sh [--dry-run] [--no-start]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.agentworkos.daemon"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$ROOT/.venv/bin/python"
DOMAIN="gui/$(id -u)"
DRY=0
START=1
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --no-start) START=0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

[ "$(uname -s)" = "Darwin" ] || { echo "launchd is macOS only; on Linux run 'scripts/awos daemon' under systemd --user" >&2; exit 1; }
if [ ! -x "$PY" ]; then
  if [ "$DRY" = 1 ]; then echo "warning: $PY not found (run scripts/install.sh first)" >&2
  else echo "$PY not found - run scripts/install.sh first" >&2; exit 1; fi
fi

xml_escape() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
R="$(printf '%s' "$ROOT" | xml_escape)"
P="$(printf '%s' "$PY" | xml_escape)"
# launchd starts jobs with a minimal PATH: pass the user's PATH so executors (claude, codex, gh, node, git) resolve.
PATHV="$(printf '%s' "$PATH" | xml_escape)"

plist() {
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$P</string><string>-X</string><string>utf8</string><string>-m</string><string>awos.cli</string><string>daemon</string>
  </array>
  <key>WorkingDirectory</key><string>$R</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$PATHV</string>
    <key>PYTHONUTF8</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$R/var/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$R/var/logs/launchd.log</string>
</dict>
</plist>
EOF
}

echo "AgentWorkOS root: $ROOT"
if [ "$DRY" = 1 ]; then
  echo "[dry-run] would write $PLIST:"
  plist
  echo "[dry-run] then: launchctl bootout $DOMAIN/$LABEL; launchctl bootstrap $DOMAIN $PLIST"
  exit 0
fi

mkdir -p "$ROOT/var/logs" "$(dirname "$PLIST")"
plist > "$PLIST.tmp"
plutil -lint "$PLIST.tmp" >/dev/null
mv "$PLIST.tmp" "$PLIST"
echo "wrote $PLIST"

# A daemon started by hand (awos ensure-running / daemon) would make the launchd copy exit at once: stop it first.
"$ROOT/scripts/awos" stop >/dev/null 2>&1 || true
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if [ "$START" = 1 ]; then
  launchctl bootstrap "$DOMAIN" "$PLIST"
  echo "loaded $LABEL (running: launchctl print $DOMAIN/$LABEL)"
else
  echo "not loaded (--no-start); it loads at next login, or run: launchctl bootstrap $DOMAIN $PLIST"
fi
