#!/usr/bin/env bash
set +e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${FAST_LIVO2_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
APP="${ROOT}/fast_livo2_console/tools/jr_usb_camera_calibration.py"
CONDA="${HOME}/miniconda3/bin/conda"

cd "${ROOT}" || exit 1
if [[ ! -x "${CONDA}" ]]; then
  echo "Conda not found: ${CONDA}"
  exit 1
fi

"${CONDA}" run -n fast_livo2 python "${APP}" "$@"
code=$?

echo
echo "JR USB camera calibration exited with code ${code}."
echo "Press Enter to close this window."
read -r _
exit "${code}"
