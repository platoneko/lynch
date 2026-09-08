#!/usr/bin/env bash
# Install/update weekday 16:13 lynch daily_bands cron (Asia/Shanghai).
# User crontab — no sudo. Idempotent via marker; re-run updates PATH line.
set -euo pipefail

REPO="/home/neko/plat1ko/lynch"
PY="${PYTHON:-/home/neko/miniconda3/bin/python3}"
CRON_PATH="/home/neko/.local/bin:/home/neko/miniconda3/bin:/usr/bin:/bin"
LOG_DIR="${REPO}/_local/tmp"
MARKER="# lynch-daily-bands"
LINE="13 16 * * 1-5 cd ${REPO} && PATH=${CRON_PATH} ${PY} scripts/daily_bands.py >> ${LOG_DIR}/daily_bands.log 2>&1 ${MARKER}"

mkdir -p "${LOG_DIR}"
echo "=== proposed crontab line ==="
echo "${LINE}"
echo

EXISTING="$(crontab -l 2>/dev/null || true)"
if echo "${EXISTING}" | grep -F "${MARKER}" >/dev/null; then
  EXISTING="$(echo "${EXISTING}" | grep -Fv "${MARKER}")"
  echo "Will update: ${MARKER}"
else
  echo "Will install: ${MARKER}"
fi

{
  printf '%s\n' "${EXISTING}"
  echo "${LINE}"
} | sed '/./,$!d' | crontab -
echo "Installed. Verify with: crontab -l | grep lynch-daily-bands"
