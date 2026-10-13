#!/usr/bin/env bash
set -euo pipefail

# Runtime-only credential bridge. Values are read from Kubernetes Secrets and
# remain in the child process environment; they are never put in arguments,
# receipts, or logs.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KUBECONFIG_PATH="${KUBECONFIG_PATH:-/srv/research-ops/tools/cluster-config}"
DATAWHEEL_AUTH_NAMESPACE="${DATAWHEEL_AUTH_NAMESPACE:-rl-training}"
DATAWHEEL_AUTH_OBJECT="${DATAWHEEL_AUTH_OBJECT:-openreward-miaoda-auth}"
DATAWHEEL_AUTH_FIELD="${DATAWHEEL_AUTH_FIELD:-datawheel_token}"
GOBLIN_AUTH_FIELD="${GOBLIN_AUTH_FIELD:-goblin_token}"

read_secret() {
  local field="$1"
  PYTHONPATH="$ROOT/backend" "$ROOT/.venv/bin/python" \
    "$ROOT/scripts/read_k8s_secret_value.py" \
    --kubeconfig "$KUBECONFIG_PATH" \
    --namespace "$DATAWHEEL_AUTH_NAMESPACE" \
    --name "$DATAWHEEL_AUTH_OBJECT" \
    --key "$field"
}

export DATAWHEEL_TOKEN="$(read_secret "$DATAWHEEL_AUTH_FIELD")"
export GOBLIN_REWARD_API_TOKEN="$(read_secret "$GOBLIN_AUTH_FIELD")"
[[ -n "$DATAWHEEL_TOKEN" ]] || { echo "Datawheel token is empty" >&2; exit 1; }
[[ -n "$GOBLIN_REWARD_API_TOKEN" ]] || { echo "Goblin token is empty" >&2; exit 1; }
trap 'unset DATAWHEEL_TOKEN GOBLIN_REWARD_API_TOKEN' EXIT

exec env PYTHONPATH="$ROOT/backend" "$ROOT/.venv/bin/python" \
  "$ROOT/scripts/watch_batch_then_goblin_reward.py" "$@"
