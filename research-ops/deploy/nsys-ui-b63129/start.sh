#!/usr/bin/env bash
set -Eeuo pipefail

REPORT_ROOT=${REPORT_ROOT:-/reports}
DEFAULT_RANK=${DEFAULT_RANK:-0}
SCREEN_GEOMETRY=${SCREEN_GEOMETRY:-1920x1080x24}
DISPLAY_NUM=${DISPLAY_NUM:-99}

export DISPLAY=":${DISPLAY_NUM}"

mkdir -p "${HOME}/.cache" "${HOME}/.config" /rank-reports
find /rank-reports -maxdepth 1 -type l -delete

shopt -s nullglob
reports=("${REPORT_ROOT}"/actor-rank*/rank-*.nsys-rep)
if (( ${#reports[@]} == 0 )); then
  echo "No .nsys-rep files found below ${REPORT_ROOT}" >&2
  exit 1
fi

for report in "${reports[@]}"; do
  ln -s "${report}" "/rank-reports/$(basename "${report}")"
done

printf -v default_report '/rank-reports/rank-%02d.nsys-rep' "${DEFAULT_RANK}"
if [[ ! -r "${default_report}" ]]; then
  echo "Default report is not readable: ${default_report}" >&2
  exit 1
fi

rm -f "/tmp/.X${DISPLAY_NUM}-lock"

pids=()
cleanup() {
  if (( ${#pids[@]} > 0 )); then
    kill "${pids[@]}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

Xvfb "${DISPLAY}" -screen 0 "${SCREEN_GEOMETRY}" -nolisten tcp -ac \
  > /tmp/xvfb.log 2>&1 &
pids+=("$!")

for _ in $(seq 1 50); do
  if xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done
xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1

openbox-session > /tmp/openbox.log 2>&1 &
pids+=("$!")

x11vnc -display "${DISPLAY}" -forever -shared -nopw -localhost -rfbport 5900 \
  > /tmp/x11vnc.log 2>&1 &
pids+=("$!")

websockify --web=/usr/share/novnc 6080 localhost:5900 \
  > /tmp/websockify.log 2>&1 &
pids+=("$!")

echo "Launching Nsight Systems UI with ${#reports[@]} reports; default=${default_report}"
nsys-ui "${default_report}" > /tmp/nsys-ui.log 2>&1 &
nsys_pid=$!
pids+=("${nsys_pid}")

wait "${nsys_pid}"
