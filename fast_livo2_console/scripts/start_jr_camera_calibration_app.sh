#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="${FAST_LIVO2_DEPLOY_DIR:-${HOME}/fast_livo2_deploy}"
OUTPUT_ROOT="${FAST_LIVO2_CAMERA_CALIB_DIR:-${HOME}/fast_livo2_data/calib/camera_intrinsics/jr_opencv}"
LOG_DIR="${FAST_LIVO2_LOG_DIR:-${HOME}/fast_livo2_data/output/jr_camera_calib}"
CAMERA_CONTAINER="${CAMERA_CONTAINER:-jr_camera}"
APP_CONTAINER="${APP_CONTAINER:-jr_camera_calibration_app}"
DISPLAY_VALUE="${DISPLAY:-:0}"
SESSION_DIR="${OUTPUT_ROOT}/$(date +%Y%m%d-%H%M%S)"
APP_SCRIPT="${FAST_LIVO2_CAMERA_CALIB_APP:-}"

mkdir -p "${OUTPUT_ROOT}" "${LOG_DIR}" "${SESSION_DIR}"
cd "${DEPLOY_DIR}"

find_app_script() {
  local candidates=()
  if [[ -n "${APP_SCRIPT}" ]]; then
    candidates+=("${APP_SCRIPT}")
  fi
  candidates+=(
    "${DEPLOY_DIR}/jr_camera_calibration_app.py"
    "${DEPLOY_DIR}/tools/jr_camera_calibration_app.py"
    "${DEPLOY_DIR}/console/tools/jr_camera_calibration_app.py"
    "${HOME}/fast_livo2_data/tools/jr_camera_calibration_app.py"
    "${HOME}/fast_livo2_ws/src/jr_fastlivo_validation/scripts/jr_camera_calibration_app.py"
  )

  local path
  for path in "${candidates[@]}"; do
    if [[ -f "${path}" ]]; then
      APP_SCRIPT="${path}"
      return 0
    fi
  done

  echo "[JR] ERROR: jr_camera_calibration_app.py was not found." >&2
  echo "[JR] Checked deploy root, deploy tools, data tools, and catkin workspace." >&2
  return 1
}

prepare_app_script_for_container() {
  local runtime_tools="${HOME}/fast_livo2_data/tools"
  local runtime_app="${runtime_tools}/jr_camera_calibration_app.py"
  mkdir -p "${runtime_tools}"
  if [[ "${APP_SCRIPT}" != "${runtime_app}" ]]; then
    cp "${APP_SCRIPT}" "${runtime_app}"
  fi
  APP_SCRIPT="${runtime_app}"
}

docker_exec_ros() {
  local container="$1"
  shift
  docker exec "${container}" bash -lc "source /opt/ros/noetic/setup.bash; source /home/jr/fast_livo2_ws/devel/setup.bash; $*"
}

wait_for_topic() {
  local container="$1"
  local topic="$2"
  local timeout_sec="${3:-30}"
  echo "[JR] waiting for ${topic}..."
  docker_exec_ros "${container}" "timeout ${timeout_sec}s bash -lc 'until rostopic list 2>/dev/null | grep -qx \"${topic}\"; do sleep 0.5; done'"
  docker_exec_ros "${container}" "timeout 2s rostopic echo -n 1 ${topic} >/dev/null" || true
}

prepare_display() {
  local auth_file=""
  auth_file="$(ls /run/user/1000/.mutter-Xwaylandauth.* 2>/dev/null | head -1 || true)"
  if [[ -z "${auth_file}" && -f "${HOME}/.Xauthority" ]]; then
    auth_file="${HOME}/.Xauthority"
  fi

  export DISPLAY="${DISPLAY_VALUE}"
  if [[ -n "${auth_file}" ]]; then
    export XAUTHORITY="${auth_file}"
  fi

  if command -v xhost >/dev/null 2>&1; then
    xhost +local:docker >/dev/null 2>&1 || true
  fi
}

cleanup_app() {
  docker rm -f "${APP_CONTAINER}" >/dev/null 2>&1 || true
}

echo "[JR] Starting JR OpenCV camera calibration app..."
echo "[JR] Output session: ${SESSION_DIR}"
prepare_display
cleanup_app
find_app_script
prepare_app_script_for_container

if ! docker ps --format '{{.Names}}' | grep -qx "${CAMERA_CONTAINER}"; then
  echo "[JR] Starting Hikrobot camera in continuous calibration mode..."
  docker rm -f "${CAMERA_CONTAINER}" >/dev/null 2>&1 || true
  docker compose run -d --name "${CAMERA_CONTAINER}" fast-livo2 bash -lc "
source /opt/ros/noetic/setup.bash
source /home/jr/fast_livo2_ws/devel/setup.bash
roslaunch jr_fastlivo_validation hikrobot_camera_continuous.launch
" >"${LOG_DIR}/camera.container"
  sleep 3
else
  echo "[JR] Camera container already running: ${CAMERA_CONTAINER}"
fi

wait_for_topic "${CAMERA_CONTAINER}" "/left_camera/image" 30

echo "[JR] Launching calibration window."
echo "[JR] Move the checkerboard; auto capture and auto solve are enabled."
echo "[JR] Touch buttons are available for Pause Auto, Solve Now, Reset, and Quit."
echo "[JR] App script: ${APP_SCRIPT}"

docker compose run --rm --name "${APP_CONTAINER}" fast-livo2 bash -lc "
export DISPLAY=${DISPLAY}
export QT_X11_NO_MITSHM=1
source /opt/ros/noetic/setup.bash
source /home/jr/fast_livo2_ws/devel/setup.bash
python3 ${APP_SCRIPT} \
  --image-topic /left_camera/image \
  --output-dir ${SESSION_DIR} \
  --inner-corners 11x8 \
  --square-size 0.025 \
  --target-count 40 \
  --min-count 25 \
  --preview-hz 5 \
  --preview-width 960 \
  --preview-height 700 \
  --detect-hz 2 \
  --detect-width 900
" 2>&1 | tee "${LOG_DIR}/calibration-app-$(date +%Y%m%d-%H%M%S).log"

echo "[JR] Calibration app closed."
echo "[JR] Session files:"
ls -lh "${SESSION_DIR}" || true
