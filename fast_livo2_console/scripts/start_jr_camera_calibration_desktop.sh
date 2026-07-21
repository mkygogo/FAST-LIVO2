#!/usr/bin/env bash
set +e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"${SCRIPT_DIR}/start_jr_camera_calibration_app.sh" "$@"
code=$?

echo
echo "JR camera calibration exited with code ${code}."
echo "Press Enter to close this window."
read -r _
exit "${code}"
