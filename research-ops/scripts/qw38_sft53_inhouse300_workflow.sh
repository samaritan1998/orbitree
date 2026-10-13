#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
MANIFEST=${QW38_SFT53_WORKFLOW_MANIFEST:-${PROJECT_ROOT}/config/qw38-sft53-inhouse300-rollout-reward.yaml}
OUTPUT_ROOT=${PROJECT_ROOT}/outputs/demo-tuned-pipeline
PID_FILE=${OUTPUT_ROOT}/workflow.pid
LOG_FILE=${OUTPUT_ROOT}/supervisor.log
CONTRACT_MANIFEST=${PROJECT_ROOT}/config/qw38-sft53-supervisor-contract.json
AUTH_KUBECONFIG=${QW38_SFT53_AUTH_KUBECONFIG:-/srv/research-ops/tools/cluster-config}
AUTH_NAMESPACE=${QW38_SFT53_AUTH_NAMESPACE:-rl-training}
AUTH_SECRET=${QW38_SFT53_AUTH_SECRET:-openreward-miaoda-auth}

usage() {
  echo "usage: $0 {validate|run|start|stop|status|migrate-state|attach-job <actual-job-name>}" >&2
}

load_primary_job_if_missing() {
  if [[ -n "${QW38_SFT53_LTP_JOB_NAME:-}" ]]; then
    return
  fi
  QW38_SFT53_LTP_JOB_NAME=$(
    python3 - "${OUTPUT_ROOT}/state.json" "${OUTPUT_ROOT}/submitted_jobs.tsv" <<'PY'
import csv
import json
import pathlib
import sys

state_path = pathlib.Path(sys.argv[1])
ledger_path = pathlib.Path(sys.argv[2])
if state_path.is_file():
    value = str(json.loads(state_path.read_text(encoding="utf-8")).get("actual_job_name") or "")
    if value:
        print(value)
        raise SystemExit(0)
if ledger_path.is_file():
    with ledger_path.open(encoding="utf-8", newline="") as stream:
        row = next(csv.DictReader(stream, delimiter="\t"), None)
    if row and row.get("actual_job_name"):
        print(row["actual_job_name"])
PY
  )
  export QW38_SFT53_LTP_JOB_NAME
}

require_runtime_environment() {
  local name
  for name in \
    QW38_SFT53_LTP_JOB_NAME \
    AGENTOS_MODEL_API_KEY \
    DATAWHEEL_TOKEN \
    DATAWHEEL_GENERATED_USER_ID \
    GOBLIN_REWARD_API_TOKEN; do
    if [[ -z "${!name:-}" ]]; then
      echo "ERROR: required environment variable is empty: ${name}" >&2
      return 1
    fi
  done
}

load_secret_if_missing() {
  local variable_name=$1
  local secret_field=$2
  local value
  if [[ -n "${!variable_name:-}" ]]; then
    return
  fi
  value=$(
    cd "${PROJECT_ROOT}"
    uv run python scripts/read_k8s_secret_value.py \
      --kubeconfig "${AUTH_KUBECONFIG}" \
      --namespace "${AUTH_NAMESPACE}" \
      --name "${AUTH_SECRET}" \
      --key "${secret_field}"
  )
  [[ -n "${value}" ]] || { echo "ERROR: runtime credential reference is empty: ${variable_name}" >&2; return 1; }
  printf -v "${variable_name}" '%s' "${value}"
  export "${variable_name}"
  unset value
}

prepare_runtime_environment() {
  load_primary_job_if_missing
  if [[ -z "${AGENTOS_MODEL_API_KEY:-}" ]]; then
    [[ -r /proc/sys/kernel/random/uuid ]] || { echo "ERROR: runtime UUID source is unavailable" >&2; return 1; }
    AGENTOS_MODEL_API_KEY=$(</proc/sys/kernel/random/uuid)
    export AGENTOS_MODEL_API_KEY
  fi
  load_secret_if_missing DATAWHEEL_TOKEN datawheel_token
  load_secret_if_missing DATAWHEEL_GENERATED_USER_ID generated_user_id
  load_secret_if_missing GOBLIN_REWARD_API_TOKEN goblin_token
  require_runtime_environment
}

run_workflow() {
  cd "${PROJECT_ROOT}"
  exec uv run python scripts/run_qw38_inhouse300_workflow.py \
    --manifest "${MANIFEST}" \
    --job-name "${QW38_SFT53_LTP_JOB_NAME}"
}

validate_workflow() {
  cd "${PROJECT_ROOT}"
  python3 /root/.codex/skills/miaoda-agentos-rollout-supervisor/scripts/validate_supervisor_manifest.py \
    "${CONTRACT_MANIFEST}"
  uv run python scripts/run_qw38_inhouse300_workflow.py \
    --manifest "${MANIFEST}" --dry-run
}

action=${1:-}
case "${action}" in
  validate)
    validate_workflow
    ;;
  attach-job)
    [[ $# -eq 2 && -n "${2:-}" ]] || { usage; exit 2; }
    cd "${PROJECT_ROOT}"
    exec uv run python scripts/run_qw38_inhouse300_workflow.py \
      --manifest "${MANIFEST}" --attach-job "$2"
    ;;
  migrate-state)
    load_primary_job_if_missing
    [[ -n "${QW38_SFT53_LTP_JOB_NAME:-}" ]] || {
      echo "ERROR: primary LTP job identity is unavailable" >&2
      exit 1
    }
    cd "${PROJECT_ROOT}"
    exec uv run python scripts/run_qw38_inhouse300_workflow.py \
      --manifest "${MANIFEST}" \
      --job-name "${QW38_SFT53_LTP_JOB_NAME}" \
      --migrate-state
    ;;
  run)
    validate_workflow >/dev/null
    prepare_runtime_environment
    run_workflow
    ;;
  start)
    validate_workflow >/dev/null
    prepare_runtime_environment
    mkdir -p "${OUTPUT_ROOT}"
    if [[ -s "${PID_FILE}" ]]; then
      existing_pid=$(<"${PID_FILE}")
      if [[ "${existing_pid}" =~ ^[0-9]+$ ]] && kill -0 "${existing_pid}" 2>/dev/null; then
        echo "workflow already running: pid=${existing_pid}"
        exit 0
      fi
    fi
    cd "${PROJECT_ROOT}"
    nohup setsid uv run python scripts/run_qw38_inhouse300_workflow.py \
      --manifest "${MANIFEST}" \
      --job-name "${QW38_SFT53_LTP_JOB_NAME}" \
      >"${LOG_FILE}" 2>&1 </dev/null &
    workflow_pid=$!
    printf '%s\n' "${workflow_pid}" >"${PID_FILE}"
    echo "workflow started: pid=${workflow_pid} log=${LOG_FILE}"
    ;;
  stop)
    if [[ ! -s "${PID_FILE}" ]]; then
      echo "workflow is not running"
      exit 0
    fi
    workflow_pid=$(<"${PID_FILE}")
    if [[ ! "${workflow_pid}" =~ ^[0-9]+$ ]] || ! kill -0 "${workflow_pid}" 2>/dev/null; then
      echo "workflow is not running"
      exit 0
    fi
    process_command=$(tr '\0' ' ' <"/proc/${workflow_pid}/cmdline" 2>/dev/null || true)
    if [[ "${process_command}" != *"run_qw38_inhouse300_workflow.py"* ]] || \
       [[ "${process_command}" != *"qw38-sft53-inhouse300-rollout-reward.yaml"* ]]; then
      echo "ERROR: pid file does not identify this supervisor" >&2
      exit 1
    fi
    kill -TERM "${workflow_pid}"
    for _ in $(seq 1 30); do
      if ! kill -0 "${workflow_pid}" 2>/dev/null; then
        echo "workflow stopped: pid=${workflow_pid}"
        exit 0
      fi
      sleep 1
    done
    echo "ERROR: supervisor did not stop after SIGTERM" >&2
    exit 1
    ;;
  status)
    if [[ -s "${PID_FILE}" ]]; then
      workflow_pid=$(<"${PID_FILE}")
      if [[ "${workflow_pid}" =~ ^[0-9]+$ ]] && kill -0 "${workflow_pid}" 2>/dev/null; then
        process_status=running
      else
        process_status=stopped
      fi
    else
      process_status=not_started
      workflow_pid=none
    fi
    echo "process=${process_status} pid=${workflow_pid}"
    if [[ -s "${OUTPUT_ROOT}/state.json" ]]; then
      python3 - "${OUTPUT_ROOT}/state.json" <<'PY'
import json
import sys

state = json.load(open(sys.argv[1], encoding="utf-8"))
for key in (
    "phase",
    "actual_job_name",
    "requested_replicas",
    "ready_replicas",
    "endpoint_count",
    "effective_concurrency",
    "applied_concurrency",
    "batch_task_id",
    "batch_status",
    "rollout_phase",
    "goblin_job_id",
    "updated_at",
):
    if state.get(key) is not None:
        print(f"{key}={state[key]}")
PY
    fi
    if [[ -s "${OUTPUT_ROOT}/submitted_jobs.tsv" ]]; then
      python3 - "${OUTPUT_ROOT}/submitted_jobs.tsv" <<'PY'
import csv
import sys

with open(sys.argv[1], encoding="utf-8", newline="") as stream:
    rows = list(csv.DictReader(stream, delimiter="\t"))
print(f"ledger_jobs={len(rows)}")
for row in rows:
    print(
        "job=" + str(row.get("actual_job_name") or "")
        + " cluster=" + str(row.get("cluster") or "")
        + " preclean=" + str(row.get("gateway_preclean") or "")
    )
PY
    fi
    ;;
  *)
    usage
    exit 2
    ;;
esac
