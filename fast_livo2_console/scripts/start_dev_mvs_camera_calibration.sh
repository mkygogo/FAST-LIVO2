#!/usr/bin/env bash
set +e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${FAST_LIVO2_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
APP="${ROOT}/fast_livo2_console/tools/jr_mvs_camera_calibration.py"
CONDA="${HOME}/miniconda3/bin/conda"

export MVCAM_SDK_PATH="${MVCAM_SDK_PATH:-/opt/MVS}"
export MVCAM_COMMON_RUNENV="${MVCAM_COMMON_RUNENV:-/opt/MVS/lib}"
export MVCAM_GENICAM_CLPROTOCOL="${MVCAM_GENICAM_CLPROTOCOL:-/opt/MVS/lib/CLProtocol}"
export ALLUSERSPROFILE="${ALLUSERSPROFILE:-/opt/MVS/MVFG}"
export LD_LIBRARY_PATH="/opt/MVS/lib/64:/opt/MVS/lib/32:${LD_LIBRARY_PATH}"
export JR_CAMERA_NAME="jr_mvs_camera"
export JR_CALIB_REPORT_TITLE="JR MVS OpenCV camera calibration"

cd "${ROOT}" || exit 1
if [[ ! -x "${CONDA}" ]]; then
  echo "Conda not found: ${CONDA}"
  exit 1
fi

"${CONDA}" run -n fast_livo2 python "${APP}" "$@"
code=$?

echo
echo "JR MVS camera calibration exited with code ${code}."
echo "Press Enter to close this window."
read -r _
exit "${code}"
