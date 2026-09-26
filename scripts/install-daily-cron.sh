#!/usr/bin/env bash
# Install/update lynch user crontab lines (Asia/Shanghai wall clock).
# User crontab — no sudo. Idempotent via markers; re-run updates PATH lines.
#
#   lynch-daily-bands-am       weekdays 09:03  — morning zone digest
#   lynch-daily-bands          weekdays 16:13  — afternoon zone digest
#   lynch-earnings-ha          weekdays 21:30  — H/A 财报可看 (BJT day)
#   lynch-earnings-us          Tue–Sat  06:00  — US 财报可看 (covers Fri AMC → Sat BJT)
set -euo pipefail

REPO="/home/neko/plat1ko/lynch"
PY="${PYTHON:-/home/neko/miniconda3/bin/python3}"
CRON_PATH="/home/neko/.local/bin:/home/neko/miniconda3/bin:/usr/bin:/bin"
LOG_DIR="${REPO}/_local/tmp"

MARKER_BANDS_AM="# lynch-daily-bands-am"
MARKER_BANDS="# lynch-daily-bands"
MARKER_HA="# lynch-earnings-ha"
MARKER_US="# lynch-earnings-us"

LINE_BANDS_AM="3 9 * * 1-5 cd ${REPO} && PATH=${CRON_PATH} ${PY} scripts/daily_bands.py >> ${LOG_DIR}/daily_bands.log 2>&1 ${MARKER_BANDS_AM}"
LINE_BANDS="13 16 * * 1-5 cd ${REPO} && PATH=${CRON_PATH} ${PY} scripts/daily_bands.py >> ${LOG_DIR}/daily_bands.log 2>&1 ${MARKER_BANDS}"
LINE_HA="30 21 * * 1-5 cd ${REPO} && PATH=${CRON_PATH} ${PY} scripts/notify_earnings_available.py --market ha >> ${LOG_DIR}/earnings_available_ha.log 2>&1 ${MARKER_HA}"
LINE_US="0 6 * * 2-6 cd ${REPO} && PATH=${CRON_PATH} ${PY} scripts/notify_earnings_available.py --market us >> ${LOG_DIR}/earnings_available_us.log 2>&1 ${MARKER_US}"

mkdir -p "${LOG_DIR}"
echo "=== proposed crontab lines ==="
echo "${LINE_BANDS_AM}"
echo "${LINE_BANDS}"
echo "${LINE_HA}"
echo "${LINE_US}"
echo

EXISTING="$(crontab -l 2>/dev/null || true)"
for marker in "${MARKER_BANDS_AM}" "${MARKER_BANDS}" "${MARKER_HA}" "${MARKER_US}"; do
  if echo "${EXISTING}" | grep -F "${marker}" >/dev/null; then
    EXISTING="$(echo "${EXISTING}" | grep -Fv "${marker}")"
    echo "Will update: ${marker}"
  else
    echo "Will install: ${marker}"
  fi
done

{
  printf '%s\n' "${EXISTING}"
  echo "${LINE_BANDS_AM}"
  echo "${LINE_BANDS}"
  echo "${LINE_HA}"
  echo "${LINE_US}"
} | sed '/./,$!d' | crontab -

echo "Installed. Verify with:"
echo "  crontab -l | grep lynch-"
