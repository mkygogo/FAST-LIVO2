#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${FAST_LIVO2_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
APP="${ROOT}/fast_livo2_console/tools/jr_solve_camera_calibration.py"
PY="${HOME}/miniconda3/envs/fast_livo2/bin/python"

export JR_CAMERA_NAME="jr_mvs_camera"
export JR_CALIB_REPORT_TITLE="JR MVS OpenCV camera calibration"

cd "${ROOT}"
if [[ ! -x "${PY}" ]]; then
  echo "Python env not found: ${PY}" >&2
  exit 1
fi

"${PY}" "${APP}" "$@"
