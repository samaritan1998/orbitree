#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -x .venv/bin/rops ]]; then
  echo "Orbitree is not installed. Run scripts/install-host.sh first." >&2
  exit 1
fi

exec .venv/bin/rops serve "$@"
