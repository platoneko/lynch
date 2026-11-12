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
PY="${PYTHON:-${REPO}/app/server/.venv/bin/python}"
VALUATION_DB="${LYNCH_VALUATION_DB:?必须提供已保存估值结果库的绝对路径}"
[[ "${VALUATION_DB}" = /* ]] || { echo '估值结果库路径必须是绝对路径' >&2; exit 1; }
[[ "${VALUATION_DB}" != *%* && "${VALUATION_DB}" != *$'\n'* ]] || { echo '结果库路径不能含百分号或换行' >&2; exit 1; }
[[ "${PY}" != *%* && "${PY}" != *$'\n'* ]] || { echo 'Python 路径不能含百分号或换行' >&2; exit 1; }
printf -v QUOTED_PY '%q' "${PY}"
printf -v QUOTED_DB '%q' "${VALUATION_DB}"
VALUATION_ENV="LYNCH_VALUATION_DB=${QUOTED_DB}"
if [[ -n "${LYNCH_VALUATION_SOURCES:-}" ]]; then
  [[ "${LYNCH_VALUATION_SOURCES}" = /* ]] || { echo '估值来源配置路径必须是绝对路径' >&2; exit 1; }
  [[ "${LYNCH_VALUATION_SOURCES}" != *%* && "${LYNCH_VALUATION_SOURCES}" != *$'\n'* ]] || { echo '来源配置路径不能含百分号或换行' >&2; exit 1; }
  printf -v QUOTED_SOURCES '%q' "${LYNCH_VALUATION_SOURCES}"
  VALUATION_ENV+=" LYNCH_VALUATION_SOURCES=${QUOTED_SOURCES}"
fi
CRON_PATH="/home/neko/.local/bin:/home/neko/miniconda3/bin:/usr/bin:/bin"
LOG_DIR="${REPO}/_local/tmp"

MARKER_BANDS_AM="# lynch-daily-bands-am"
MARKER_BANDS="# lynch-daily-bands"
RETIRED_MARKERS=("# lynch-earnings-ha" "# lynch-earnings-us")

LINE_BANDS_AM="3 9 * * 1-5 cd ${REPO} && PATH=${CRON_PATH} ${VALUATION_ENV} ${QUOTED_PY} scripts/daily_bands.py >> ${LOG_DIR}/daily_bands.log 2>&1 ${MARKER_BANDS_AM}"
LINE_BANDS="13 16 * * 1-5 cd ${REPO} && PATH=${CRON_PATH} ${VALUATION_ENV} ${QUOTED_PY} scripts/daily_bands.py >> ${LOG_DIR}/daily_bands.log 2>&1 ${MARKER_BANDS}"

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
