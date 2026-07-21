#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-status}"
DEPLOY_DIR="${FAST_LIVO2_DEPLOY_DIR:-${HOME}/fast_livo2_deploy}"
DATA_ROOT="${FAST_CALIB2_DATA_ROOT:-${HOME}/fast_livo2_data/calib/fast_calib2_mv_cs050}"
CONFIG="${FAST_CALIB2_CAMERA_CONFIG:-${DATA_ROOT}/config/hikrobot_camera_fast_calib2.yaml}"
LOG_DIR="${DATA_ROOT}/logs"
CAMERA_CONTAINER="${FAST_CALIB2_CAMERA_CONTAINER:-jr_hik_trig_view}"
LIDAR_CONTAINER="${FAST_CALIB2_LIDAR_CONTAINER:-jr_mid360_view}"
TRIGGER_SOURCE="${FAST_CALIB2_TRIGGER_SOURCE:-Line0}"
TRIGGER_ACTIVATION="${FAST_CALIB2_TRIGGER_ACTIVATION:-RisingEdge}"

log() {
  echo "[FAST-Calib2 devices] $*"
}

die() {
  echo "[FAST-Calib2 devices] ERROR: $*" >&2
  exit 1
}

container_running() {
  docker ps --format '{{.Names}}' | grep -qx "$1"
}

mapping_running() {
  local name
  for name in fast_livo2_mapping jr_lidar_mapping jr_fastlivo_rviz; do
    if container_running "${name}"; then
      echo "${name}"
      return 0
    fi
  done
  return 1
}

ros_exec() {
  local container="$1"
  shift
  docker exec "${container}" bash -lc \
    "source /opt/ros/noetic/setup.bash; source /home/jr/fast_livo2_ws/devel/setup.bash; $*"
}

topic_has_message() {
  local container="$1"
  local topic="$2"
  local seconds="${3:-5}"
  container_running "${container}" || return 1
  ros_exec "${container}" "timeout ${seconds}s rostopic echo -n 1 '${topic}' >/dev/null 2>&1"
}

wait_for_message() {
  local container="$1"
  local topic="$2"
  local seconds="${3:-30}"
  local deadline=$((SECONDS + seconds))
  log "waiting for ${topic} (up to ${seconds}s)..."
  while (( SECONDS < deadline )); do
    container_running "${container}" || break
    if topic_has_message "${container}" "${topic}" 2; then
      log "ready: ${topic}"
      return 0
    fi
    sleep 1
  done
  docker logs --tail 100 "${container}" 2>&1 || true
  die "no message received from ${topic}"
}

status() {
  local ok=1
  echo "=== FAST-Calib2 device status ==="
  if lsusb | grep -q '2bdf:0001'; then
    echo "camera USB: connected (MV-CS050-10UC)"
  else
    echo "camera USB: MISSING"
    ok=0
  fi
  if ping -c 1 -W 1 192.168.1.151 >/dev/null 2>&1; then
    echo "Mid360 network: reachable"
  else
    echo "Mid360 network: UNREACHABLE"
    ok=0
  fi
  for name in "${LIDAR_CONTAINER}" "${CAMERA_CONTAINER}"; do
    if container_running "${name}"; then
      echo "container ${name}: running"
    else
      echo "container ${name}: stopped"
      ok=0
    fi
  done
  for topic in /livox/lidar /livox/imu /left_camera/image; do
    if topic_has_message "${CAMERA_CONTAINER}" "${topic}" 4; then
      echo "topic ${topic}: OK"
    else
      echo "topic ${topic}: NO DATA"
      ok=0
    fi
  done
  if topic_has_message "${CAMERA_CONTAINER}" /hikrobot_camera/frame_info 4; then
    echo "topic /hikrobot_camera/frame_info: OK"
  else
    echo "topic /hikrobot_camera/frame_info: no data (optional metadata)"
  fi
  [[ "${ok}" == "1" ]]
}

stop_devices() {
  log "stopping calibration containers..."
  docker rm -f "${CAMERA_CONTAINER}" "${LIDAR_CONTAINER}" >/dev/null 2>&1 || true
  if [[ -x "${DEPLOY_DIR}/console/scripts/livox_sleep.sh" ]]; then
    "${DEPLOY_DIR}/console/scripts/livox_sleep.sh" || true
  elif [[ -x "${DEPLOY_DIR}/livox_sleep.sh" ]]; then
    "${DEPLOY_DIR}/livox_sleep.sh" || true
  fi
  log "calibration devices stopped"
}

start_devices() {
  local active_mapping=""
  if active_mapping="$(mapping_running)"; then
    die "mapping container ${active_mapping} is running; finish mapping before calibration"
  fi
  [[ -d "${DEPLOY_DIR}" ]] || die "deployment directory not found: ${DEPLOY_DIR}"
  [[ -f "${CONFIG}" ]] || die "camera trigger config not found: ${CONFIG}"
  lsusb | grep -q '2bdf:0001' || die "MV-CS050-10UC is not connected"
  ping -c 1 -W 1 192.168.1.151 >/dev/null 2>&1 || die "Mid360 is unreachable at 192.168.1.151"
  mkdir -p "${LOG_DIR}"

  log "removing stale/conflicting device containers..."
  docker rm -f \
    "${CAMERA_CONTAINER}" "${LIDAR_CONTAINER}" \
    hikrobot_camera jr_camera mid360_driver mid360_preview_driver mid360_driver_test \
    >/dev/null 2>&1 || true

  if [[ -x "${DEPLOY_DIR}/console/scripts/livox_wake.sh" ]]; then
    "${DEPLOY_DIR}/console/scripts/livox_wake.sh" || true
  elif [[ -x "${DEPLOY_DIR}/livox_wake.sh" ]]; then
    "${DEPLOY_DIR}/livox_wake.sh" || true
  fi

  cd "${DEPLOY_DIR}"
  log "starting Mid360 driver..."
  docker compose run -d --name "${LIDAR_CONTAINER}" fast-livo2 bash -lc "
source /opt/ros/noetic/setup.bash
source /home/jr/fast_livo2_ws/devel/setup.bash
rosparam delete /run_id >/dev/null 2>&1 || true
roslaunch livox_ros_driver2 msg_MID360.launch xfer_format:=1 rviz_enable:=false
" >"${LOG_DIR}/mid360-container-$(date +%Y%m%d-%H%M%S).log" 2>&1
  wait_for_message "${LIDAR_CONTAINER}" /livox/lidar 35
  wait_for_message "${LIDAR_CONTAINER}" /livox/imu 15

  log "starting Hikrobot external-trigger camera (${TRIGGER_SOURCE}/${TRIGGER_ACTIVATION})..."
  docker compose run -d --name "${CAMERA_CONTAINER}" fast-livo2 bash -lc "
source /opt/ros/noetic/setup.bash
source /home/jr/fast_livo2_ws/devel/setup.bash
rosparam delete /run_id >/dev/null 2>&1 || true
roslaunch jr_fastlivo_validation hikrobot_camera_external_trigger.launch \\
  config:=${CONFIG} \\
  trigger_source:=${TRIGGER_SOURCE} \\
  trigger_activation:=${TRIGGER_ACTIVATION}
" >"${LOG_DIR}/camera-container-$(date +%Y%m%d-%H%M%S).log" 2>&1
  wait_for_message "${CAMERA_CONTAINER}" /left_camera/image 30
  wait_for_message "${CAMERA_CONTAINER}" /livox/lidar 10
  wait_for_message "${CAMERA_CONTAINER}" /livox/imu 10
  if topic_has_message "${CAMERA_CONTAINER}" /hikrobot_camera/frame_info 8; then
    log "ready: /hikrobot_camera/frame_info"
  else
    log "warning: frame_info has no data; image/lidar/imu recording can still continue"
  fi
  log "all required FAST-Calib2 topics are ready"
}

case "${ACTION}" in
  start)
    start_devices
    ;;
  ensure)
    status >/dev/null 2>&1 || start_devices
    ;;
  stop)
    stop_devices
    ;;
  status)
    status
    ;;
  *)
    echo "Usage: $0 {start|ensure|stop|status}" >&2
    exit 2
    ;;
esac
