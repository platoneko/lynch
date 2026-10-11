#!/usr/bin/env bash
# Install/update lynch user crontab lines (Asia/Shanghai wall clock).
# User crontab — no sudo. Idempotent via markers; re-run updates PATH lines.
#
#   lynch-daily-bands-am       weekdays 09:03  — morning zone digest
#   lynch-daily-bands          weekdays 16:13  — afternoon zone digest
#
# Retired markers are removed from the crontab on every run:
#   lynch-earnings-ha / lynch-earnings-us — Feishu earnings notifier, replaced by the app's earnings page
set -euo pipefail

REPO="/home/neko/plat1ko/lynch"
PY="${PYTHON:-/home/neko/miniconda3/bin/python3}"
CRON_PATH="/home/neko/.local/bin:/home/neko/miniconda3/bin:/usr/bin:/bin"
LOG_DIR="${REPO}/_local/tmp"

MARKER_BANDS_AM="# lynch-daily-bands-am"
MARKER_BANDS="# lynch-daily-bands"
RETIRED_MARKERS=("# lynch-earnings-ha" "# lynch-earnings-us")

LINE_BANDS_AM="3 9 * * 1-5 cd ${REPO} && PATH=${CRON_PATH} ${PY} scripts/daily_bands.py >> ${LOG_DIR}/daily_bands.log 2>&1 ${MARKER_BANDS_AM}"
LINE_BANDS="13 16 * * 1-5 cd ${REPO} && PATH=${CRON_PATH} ${PY} scripts/daily_bands.py >> ${LOG_DIR}/daily_bands.log 2>&1 ${MARKER_BANDS}"

mkdir -p "${LOG_DIR}"
echo "=== proposed crontab lines ==="
echo "${LINE_BANDS_AM}"
echo "${LINE_BANDS}"
echo

EXISTING="$(crontab -l 2>/dev/null || true)"
for marker in "${MARKER_BANDS_AM}" "${MARKER_BANDS}"; do
  if echo "${EXISTING}" | grep -F "${marker}" >/dev/null; then
    EXISTING="$(echo "${EXISTING}" | grep -Fv "${marker}")"
    echo "Will update: ${marker}"
  else
    echo "Will install: ${marker}"
  fi
done
for marker in "${RETIRED_MARKERS[@]}"; do
  if echo "${EXISTING}" | grep -F "${marker}" >/dev/null; then
    EXISTING="$(echo "${EXISTING}" | grep -Fv "${marker}")"
    echo "Will remove (retired): ${marker}"
  fi
done

{
  printf '%s\n' "${EXISTING}"
  echo "${LINE_BANDS_AM}"
  echo "${LINE_BANDS}"
} | sed '/./,$!d' | crontab -

echo "Installed. Verify with:"
echo "  crontab -l | grep lynch-"
