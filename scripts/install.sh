#!/usr/bin/env bash
# AgentWorkOS bootstrap for macOS / Linux (counterpart of install.ps1). Idempotent: safe to re-run at any time.
#
#   1. checks uv (prints how to install it if missing)
#   2. creates .venv with Python 3.13 (uv venv --python 3.13) unless it already exists
#   3. uv pip install -e .[dev]
#   4. creates the var/ sub-directories
#   5. checks Node >= 22.19 for pi / dsh: runtime/node/bin/node if present, else `node` on PATH (e.g. Homebrew)
#   6. optional: --service registers the launchd LaunchAgent (scripts/service-install.sh, macOS only)
#
# usage: scripts/install.sh [--service]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$ROOT/.venv/bin/python"
MIN_NODE="22.19.0"
SERVICE=0
for a in "$@"; do
  case "$a" in
    --service) SERVICE=1 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }
ok() { printf '    \033[32m%s\033[0m\n' "$1"; }
warn() { printf '    \033[33m%s\033[0m\n' "$1"; }

echo "AgentWorkOS root: $ROOT"

step "uv"
if ! command -v uv >/dev/null 2>&1; then
  warn "uv is not installed. Install it, open a new terminal, then re-run this script:"
  echo "      curl -LsSf https://astral.sh/uv/install.sh | sh     (or: brew install uv)"
  exit 1
fi
ok "$(uv --version) ($(command -v uv))"

step "Python virtualenv (.venv, Python 3.13)"
if [ -x "$VENV_PY" ]; then
  pyv="$("$VENV_PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  if [ "$pyv" != "3.13" ]; then warn ".venv uses Python $pyv (expected 3.13). Delete .venv and re-run to recreate it."
  else ok ".venv exists (Python $pyv) - kept"; fi
else
  uv venv --python 3.13 "$ROOT/.venv"
fi

step "Python dependencies (uv pip install -e .[dev])"
(cd "$ROOT" && uv pip install --python "$VENV_PY" -e '.[dev]')

step "data directories (var/)"
for d in var var/workspaces var/artifacts var/logs var/runs var/backups var/context var/inbox-drop; do
  mkdir -p "$ROOT/$d"
done
ok "var/{workspaces,artifacts,logs,runs,backups,context,inbox-drop}"

step "Node.js for pi / dsh (>= $MIN_NODE)"
NODE="$ROOT/runtime/node/bin/node"
[ -x "$NODE" ] || NODE="$(command -v node || true)"
if [ -z "$NODE" ]; then
  warn "no Node found: pi / dsh stay unavailable (install Node >= $MIN_NODE, e.g. brew install node)"
else
  nv="$("$NODE" --version | sed 's/^v//')"
  if [ "$(printf '%s\n%s\n' "$MIN_NODE" "$nv" | sort -V | head -1)" = "$MIN_NODE" ]; then ok "$NODE = v$nv"
  else warn "$NODE is v$nv, older than $MIN_NODE: pi / dsh need a newer Node"; fi
fi

if [ "$SERVICE" = 1 ]; then
  step "launchd LaunchAgent"
  "$ROOT/scripts/service-install.sh"
fi

step "done"
echo "    CLI:       $ROOT/scripts/awos status"
echo "    daemon:    $ROOT/scripts/awos daemon            (foreground)  |  $ROOT/scripts/awos ensure-running  (background)"
echo "    service:   $ROOT/scripts/service-install.sh     (start at login, restart on crash; macOS)"
echo "    runbook:   $ROOT/docs/runbook.md"
