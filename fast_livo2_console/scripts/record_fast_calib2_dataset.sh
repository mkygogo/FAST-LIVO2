#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="${FAST_LIVO2_DEPLOY_DIR:-${HOME}/fast_livo2_deploy}"
DATA_ROOT="${FAST_CALIB2_DATA_ROOT:-${HOME}/fast_livo2_data/calib/fast_calib2_mv_cs050}"
INTRINSICS="${FAST_CALIB2_INTRINSICS:-${DATA_ROOT}/intrinsics/jr_mvs_fast_calib2_intrinsics.yaml}"
ROS_CONTAINER="${FAST_CALIB2_ROS_CONTAINER:-jr_hik_trig_view}"
PREVIEW_TOOL="${FAST_CALIB2_PREVIEW_TOOL:-${HOME}/fast_livo2_data/tools/fast_calib2_record_preview.py}"
DURATION_SEC="${FAST_CALIB2_RECORD_DURATION:-3}"
DISPLAY_VALUE="${DISPLAY:-:0}"

log() {
  echo "[FAST-Calib2 preview record] $*"
}

die() {
  echo "[FAST-Calib2 preview record] ERROR: $*" >&2
  exit 1
}

docker ps --format '{{.Names}}' | grep -qx "${ROS_CONTAINER}" || \
  die "camera container is not running: ${ROS_CONTAINER}"
[[ -f "${PREVIEW_TOOL}" ]] || die "preview tool not found: ${PREVIEW_TOOL}"
mkdir -p "${DATA_ROOT}/datasets"

# The desktop session owns the Xwayland display. Restrict access to the local
# root user used inside the ROS container, and remove the grant afterwards.
if command -v xhost >/dev/null 2>&1; then
  DISPLAY="${DISPLAY_VALUE}" xhost +SI:localuser:root >/dev/null 2>&1 || true
fi
cleanup() {
  if command -v xhost >/dev/null 2>&1; then
    DISPLAY="${DISPLAY_VALUE}" xhost -SI:localuser:root >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

log "opening live 1024x600 touch preview..."
docker exec \
  -e DISPLAY="${DISPLAY_VALUE}" \
  -e QT_X11_NO_MITSHM=1 \
  "${ROS_CONTAINER}" bash -lc "
source /opt/ros/noetic/setup.bash
source /home/jr/fast_livo2_ws/devel/setup.bash
python3 '${PREVIEW_TOOL}' \\
  --data-root '${DATA_ROOT}' \\
  --intrinsics '${INTRINSICS}' \\
  --duration '${DURATION_SEC}'
"
