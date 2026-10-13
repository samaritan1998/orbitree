#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_SERVICE=1
if [[ "${1:-}" == "--no-service" ]]; then
  INSTALL_SERVICE=0
fi

for command in uv npm git codex; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Missing required command: $command" >&2
    exit 1
  fi
done

cd "$ROOT"
uv sync --no-dev
if [[ "${ROPS_SKIP_WEB_INSTALL:-0}" != "1" ]]; then
  npm --prefix web ci --prefer-offline --no-audit --no-fund
fi
npm --prefix web run build
.venv/bin/rops init

if [[ ! -f .env ]]; then
  cp .env.example .env
fi

if [[ "$INSTALL_SERVICE" -eq 1 ]]; then
  if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    mkdir -p "$SERVICE_DIR"
    sed "s|@@ROOT@@|$ROOT|g" deploy/systemd/research-ops.service.in \
      > "$SERVICE_DIR/research-ops.service"
    systemctl --user daemon-reload
    systemctl --user enable --now research-ops.service
    echo "Research Ops systemd service started."
  else
    .venv/bin/rops server start
    echo "User systemd is unavailable; detached Research Ops supervisor started."
  fi
else
  echo "Run $ROOT/.venv/bin/rops server start to start Research Ops in the background."
fi
