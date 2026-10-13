#!/usr/bin/env bash
set -euo pipefail

SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_FILE="$SERVICE_DIR/research-ops.service"

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user disable --now research-ops.service 2>/dev/null || true
  rm -f "$SERVICE_FILE"
  systemctl --user daemon-reload
fi

echo "Service removed. Project files, data, and worktrees were preserved."
