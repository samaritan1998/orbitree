#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
MANIFEST=${QW38_WORKFLOW_MANIFEST:-${PROJECT_ROOT}/config/qw38-inhouse300-rollout-reward.yaml}
OUTPUT_ROOT=${PROJECT_ROOT}/outputs/demo-base-pipeline
PID_FILE=${OUTPUT_ROOT}/workflow.pid
LOG_FILE=${OUTPUT_ROOT}/supervisor.log

usage() {
  echo "usage: $0 {validate|run|start|status}" >&2
}

require_runtime_environment() {
  local name
  for name in \
    QW38_LTP_JOB_NAME \
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

run_workflow() {
  cd "${PROJECT_ROOT}"
  exec uv run python scripts/run_qw38_inhouse300_workflow.py \
    --manifest "${MANIFEST}" \
    --job-name "${QW38_LTP_JOB_NAME}"
}

action=${1:-}
case "${action}" in
  validate)
    cd "${PROJECT_ROOT}"
    exec uv run python scripts/run_qw38_inhouse300_workflow.py \
      --manifest "${MANIFEST}" --dry-run
    ;;
  run)
    require_runtime_environment
    run_workflow
    ;;
  start)
    require_runtime_environment
    mkdir -p "${OUTPUT_ROOT}"
    if [[ -s "${PID_FILE}" ]]; then
      existing_pid=$(<"${PID_FILE}")
      if [[ "${existing_pid}" =~ ^[0-9]+$ ]] && kill -0 "${existing_pid}" 2>/dev/null; then
        echo "workflow already running: pid=${existing_pid}"
        exit 0
      fi
    fi
    cd "${PROJECT_ROOT}"
    nohup uv run python scripts/run_qw38_inhouse300_workflow.py \
      --manifest "${MANIFEST}" \
      --job-name "${QW38_LTP_JOB_NAME}" \
      >"${LOG_FILE}" 2>&1 </dev/null &
    workflow_pid=$!
    printf '%s\n' "${workflow_pid}" >"${PID_FILE}"
    echo "workflow started: pid=${workflow_pid} log=${LOG_FILE}"
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
    ;;
  *)
    usage
    exit 2
    ;;
esac
