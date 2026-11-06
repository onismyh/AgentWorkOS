#!/usr/bin/env bash
# AgentWorkOS one-click bootstrap for macOS / Linux (counterpart of install.ps1). Idempotent: safe to re-run at any time.
#
#   1. uv: installed into ~/.local/bin from astral.sh if missing
#   2. .venv with Python 3.13 (uv venv --python 3.13) unless it already exists; uv pip install -e .[dev]
#   3. var/ sub-directories
#   4. Node >= 22.19 for the agent CLIs: runtime/node, else `node` on PATH; if neither, a portable Node LTS is
#      downloaded into runtime/node (SHA256 checked against nodejs.org SHASUMS256.txt). The system Node is never touched.
#   5. `awos setup`: codex / pi / dsh into runtime/ (pinned, no lifecycle scripts) + credentials already on this machine
#      (Codex login, DEEPSEEK_API_KEY, DeepSeek Harness account login) wired into var/secrets.toml (0600)
#   6. optional: --service registers the launchd LaunchAgent (scripts/service-install.sh, macOS only)
#
# usage: scripts/install.sh [--service] [--skip-runtimes]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$ROOT/.venv/bin/python"
MIN_NODE="22.19.0"
SERVICE=0
RUNTIMES=1
NODE_MAJOR=24
for a in "$@"; do
  case "$a" in
    --service) SERVICE=1 ;;
    --skip-runtimes) RUNTIMES=0 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }
ok() { printf '    \033[32m%s\033[0m\n' "$1"; }
warn() { printf '    \033[33m%s\033[0m\n' "$1"; }

echo "AgentWorkOS root: $ROOT"

step "uv"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  ok "installing uv into ~/.local/bin (https://astral.sh/uv/install.sh)"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
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

node_ok() {  # $1 = node binary; true when >= MIN_NODE
  [ -x "$1" ] || return 1
  local v; v="$("$1" --version 2>/dev/null | sed 's/^v//')" || return 1
  [ "$(printf '%s\n%s\n' "$MIN_NODE" "$v" | sort -V | head -1)" = "$MIN_NODE" ]
}

install_portable_node() {
  local os arch plat ver tmp
  case "$(uname -s)" in Darwin) os=darwin ;; Linux) os=linux ;; *) warn "unsupported OS for portable Node"; return 1 ;; esac
  case "$(uname -m)" in arm64|aarch64) arch=arm64 ;; x86_64|amd64) arch=x64 ;; *) warn "unsupported CPU for portable Node"; return 1 ;; esac
  plat="$os-$arch"
  local key="$plat"   # index.json "files" key: osx-arm64-tar / linux-x64 (the tarball itself is node-vX-darwin-arm64.tar.gz)
  [ "$os" = darwin ] && key="osx-$arch-tar"
  ver="$(curl -fsSL https://nodejs.org/dist/index.json | "$VENV_PY" -c "
import json, sys
rel = [r for r in json.load(sys.stdin) if r['version'].startswith('v$NODE_MAJOR.') and r.get('lts') and '$key' in r['files']]
print(rel[0]['version'] if rel else '')")"
  [ -n "$ver" ] || { warn "no Node v$NODE_MAJOR LTS build for $plat"; return 1; }
  tmp="$(mktemp -d)"
  ok "downloading node-$ver-$plat.tar.gz"
  curl -fsSL -o "$tmp/node.tar.gz" "https://nodejs.org/dist/$ver/node-$ver-$plat.tar.gz"
  curl -fsSL -o "$tmp/SHASUMS256.txt" "https://nodejs.org/dist/$ver/SHASUMS256.txt"
  local want got
  want="$(grep " node-$ver-$plat.tar.gz\$" "$tmp/SHASUMS256.txt" | cut -d' ' -f1)"
  got="$(shasum -a 256 "$tmp/node.tar.gz" | cut -d' ' -f1)"
  [ -n "$want" ] && [ "$want" = "$got" ] || { rm -rf "$tmp"; warn "SHA256 mismatch for node-$ver-$plat.tar.gz"; return 1; }
  rm -rf "$ROOT/runtime/node" && mkdir -p "$ROOT/runtime/node"
  tar -xzf "$tmp/node.tar.gz" -C "$ROOT/runtime/node" --strip-components 1
  rm -rf "$tmp"
  ok "runtime/node = $ver (SHA256 verified)"
}

step "Node.js for the agent CLIs (>= $MIN_NODE)"
NODE="$ROOT/runtime/node/bin/node"
node_ok "$NODE" || NODE="$(command -v node || true)"
if [ -n "$NODE" ] && node_ok "$NODE"; then
  ok "$NODE = $("$NODE" --version)"
elif [ "$RUNTIMES" = 1 ]; then
  install_portable_node || warn "no usable Node: codex / pi / dsh stay unavailable"
else
  warn "no Node >= $MIN_NODE (needed by codex / pi / dsh)"
fi

if [ "$RUNTIMES" = 1 ]; then
  step "agent runtimes + credentials (awos setup)"
  "$VENV_PY" -X utf8 -m awos.cli setup || warn "awos setup reported problems (see above); re-run: scripts/awos setup"
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
