#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ENV_FILE="${ROOT}/.env"
RECIPIENT="${1:-alice}"
PUBLIC_URL="${2:-https://research.example.com}"

read -r -p "Infoflow AppKey: " APP_KEY
read -r -s -p "Infoflow AppSecret: " APP_SECRET
printf '\n'

if [[ ! "${APP_KEY}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "AppKey contains unsupported characters" >&2
  exit 2
fi
if [[ ! "${APP_SECRET}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "AppSecret contains unsupported characters" >&2
  exit 2
fi
if [[ ! "${RECIPIENT}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "Recipient must be one email prefix" >&2
  exit 2
fi
if [[ ! "${PUBLIC_URL}" =~ ^https?://[^[:space:]]+$ ]]; then
  echo "Public URL must be an absolute HTTP(S) URL" >&2
  exit 2
fi

umask 077
touch "${ENV_FILE}"
TMP=$(mktemp "${ENV_FILE}.tmp.XXXXXX")
trap 'rm -f "${TMP}"' EXIT
awk '
  !/^(INFOFLOW_APP_KEY|INFOFLOW_APP_SECRET|ROPS_INFOFLOW_NOTIFICATIONS_ENABLED|ROPS_INFOFLOW_RECIPIENT|ROPS_INFOFLOW_PUBLIC_BASE_URL)=/
' "${ENV_FILE}" > "${TMP}"
cat >> "${TMP}" <<EOF
ROPS_INFOFLOW_NOTIFICATIONS_ENABLED=true
ROPS_INFOFLOW_RECIPIENT=${RECIPIENT}
ROPS_INFOFLOW_PUBLIC_BASE_URL=${PUBLIC_URL%/}
INFOFLOW_APP_KEY=${APP_KEY}
INFOFLOW_APP_SECRET=${APP_SECRET}
EOF
mv "${TMP}" "${ENV_FILE}"
chmod 600 "${ENV_FILE}"
trap - EXIT
unset APP_KEY APP_SECRET

echo "Infoflow notifications configured for ${RECIPIENT}."
echo "Restart Orbitree to load the new environment."
