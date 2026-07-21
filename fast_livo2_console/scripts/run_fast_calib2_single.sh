#!/usr/bin/env bash
set -euo pipefail

WS="${FAST_CALIB2_WS:-${HOME}/fast_livo2_ws}"
DATA_ROOT="${FAST_CALIB2_DATA_ROOT:-${HOME}/fast_livo2_data/calib/fast_calib2_mv_cs050}"
ROS_CONTAINER="${FAST_CALIB2_ROS_CONTAINER:-jr_hik_trig_view}"
RUN_TIMEOUT_SEC="${FAST_CALIB2_LAUNCH_TIMEOUT_SEC:-45}"
DATASET_DIR=""

usage() {
  echo "Usage: $0 [--dataset-dir DIR]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dataset-dir)
      DATASET_DIR="${2:?missing dataset dir}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

log() {
  echo "[FAST-Calib2 single] $*"
}

die() {
  echo "[FAST-Calib2 single] ERROR: $*" >&2
  exit 1
}

container_running() {
  docker ps --format '{{.Names}}' | grep -qx "$1"
}

valid_dataset() {
  local dir="$1"
  [[ -f "${dir}/image.png" && -f "${dir}/scene.bag" && \
     -f "${dir}/camera_intrinsics_fast_calib2.yaml" ]]
}

latest_dataset() {
  local root="${DATA_ROOT}/datasets"
  local dir newest="" newest_mtime=0 mtime
  [[ -d "${root}" ]] || return 1
  while IFS= read -r -d '' dir; do
    valid_dataset "${dir}" || continue
    mtime="$(stat -c '%Y' "${dir}")"
    if (( mtime > newest_mtime )); then
      newest_mtime="${mtime}"
      newest="${dir}"
    fi
  done < <(find "${root}" -mindepth 1 -maxdepth 1 -type d -print0)
  [[ -n "${newest}" ]] || return 1
  printf '%s\n' "${newest}"
}

update_config() {
  local config="$1"
  local intrinsics="$2"
  local image_path="$3"
  local bag_path="$4"
  local output_path="$5"
  python3 - "${config}" "${intrinsics}" "${image_path}" "${bag_path}" "${output_path}" <<'PY'
import pathlib
import re
import sys

config = pathlib.Path(sys.argv[1])
intrinsics = pathlib.Path(sys.argv[2])
image_path, bag_path, output_path = sys.argv[3:6]
values = {}
for line in intrinsics.read_text(encoding="utf-8").splitlines():
    if ":" not in line or line.lstrip().startswith("#"):
        continue
    key, value = (part.strip() for part in line.split(":", 1))
    if key in {"fx", "fy", "cx", "cy", "k1", "k2", "p1", "p2", "k3"}:
        values[key] = value

paths = {
    "bag_path": bag_path, "bagfile": bag_path, "bag_file": bag_path,
    "img_path": image_path, "image_path": image_path,
    "pic_path": image_path, "picture_path": image_path,
    "output_path": output_path,
}
changed = set()
lines = []
for line in config.read_text(encoding="utf-8").splitlines():
    match = re.match(r"^(\s*)([A-Za-z0-9_./-]+)(\s*:\s*)(.*)$", line)
    if not match:
        lines.append(line)
        continue
    indent, key, separator, _old = match.groups()
    clean = key.split("/")[-1]
    if clean in values:
        lines.append(f"{indent}{key}{separator}{values[clean]}")
        changed.add(clean)
    elif clean in paths:
        lines.append(f'{indent}{key}{separator}"{paths[clean]}"')
    else:
        lines.append(line)
config.write_text("\n".join(lines) + "\n", encoding="utf-8")
required = {"fx", "fy", "cx", "cy", "k1", "k2", "p1", "p2"}
missing = sorted(required - changed)
if missing:
    raise SystemExit("intrinsic keys missing from qr_params.yaml: " + ",".join(missing))
PY
}

valid_result() {
  local result="$1"
  local run_log="$2"
  [[ -s "${result}" ]] || return 1
  ! grep -Eq 'Need 4 centers, got|Number of lidar center points.*is not 4|Point cloud sizes do not match' "${run_log}" || return 1
  python3 - "${result}" <<'PY'
import re
import sys
text = open(sys.argv[1], encoding="utf-8").read()
match = re.search(r"Rcl:\s*\[([^]]+)\]", text, re.S)
if not match:
    raise SystemExit(1)
values = [float(value) for value in re.findall(r"[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?", match.group(1))]
if len(values) != 9 or sum(abs(value) for value in values) < 1.0:
    raise SystemExit(1)
PY
}

container_running "${ROS_CONTAINER}" || \
  die "ROS device container is not running: ${ROS_CONTAINER}. Start calibration devices first."

if [[ -z "${DATASET_DIR}" ]]; then
  DATASET_DIR="$(latest_dataset)" || die "no complete dataset found under ${DATA_ROOT}/datasets"
fi
[[ -d "${DATASET_DIR}" ]] || die "dataset directory not found: ${DATASET_DIR}"
valid_dataset "${DATASET_DIR}" || die "dataset is incomplete: ${DATASET_DIR}"

RESULT_ROOT="${DATA_ROOT}/results/$(date +%Y%m%d-%H%M%S)-single"
mkdir -p "${RESULT_ROOT}"
touch "${RESULT_ROOT}/start.mark"

log "Selected latest dataset by modification time: $(basename "${DATASET_DIR}")"
PKG_DIR="$(docker exec "${ROS_CONTAINER}" bash -lc \
  "source /opt/ros/noetic/setup.bash; source '${WS}/devel/setup.bash'; rospack find fast_calib" | tail -1 | tr -d '\r')"
[[ -n "${PKG_DIR}" ]] || die "rospack could not find fast_calib"
CONFIG="${PKG_DIR}/config/qr_params.yaml"
[[ -f "${CONFIG}" ]] || die "FAST-Calib2 config not found: ${CONFIG}"
cp -f "${CONFIG}" "${RESULT_ROOT}/qr_params.before.yaml"

update_config \
  "${CONFIG}" \
  "${DATASET_DIR}/camera_intrinsics_fast_calib2.yaml" \
  "${DATASET_DIR}/image.png" \
  "${DATASET_DIR}/scene.bag" \
  "${RESULT_ROOT}"
cp -f "${CONFIG}" "${RESULT_ROOT}/qr_params.used.yaml"

log "Running one-shot solver (hard timeout ${RUN_TIMEOUT_SEC}s)..."
run_started_at="$(date +%s)"
solver_log="${RESULT_ROOT}/fast_calib.log"
solver_exit_file="${RESULT_ROOT}/solver.exit"
(
  set +e
  docker exec "${ROS_CONTAINER}" bash -lc "
source /opt/ros/noetic/setup.bash
source '${WS}/devel/setup.bash'
rosparam load '${CONFIG}'
timeout --foreground --kill-after=5s '${RUN_TIMEOUT_SEC}s' \\
  rosrun fast_calib fast_calib __name:=fast_calib
" 2>&1 | tee "${solver_log}"
  printf '%s\n' "${PIPESTATUS[0]}" >"${solver_exit_file}"
) &
solver_job=$!

# FAST-Calib2 writes all outputs and then enters a ROS spin loop instead of
# exiting. As soon as its result exists, stop only that solver node so the
# desktop action returns immediately. The inner timeout remains the hard guard.
result_file="${RESULT_ROOT}/single_calib_result.txt"
result_signal_sent=0
while kill -0 "${solver_job}" >/dev/null 2>&1; do
  if [[ "${result_signal_sent}" == "0" && -s "${result_file}" ]]; then
    sleep 1
    docker exec "${ROS_CONTAINER}" bash -lc \
      "pkill -INT -x fast_calib >/dev/null 2>&1 || true" || true
    result_signal_sent=1
  fi
  sleep 0.2
done
set +e
wait "${solver_job}"
set -e
solver_code="$(cat "${solver_exit_file}" 2>/dev/null || echo 1)"
run_elapsed="$(( $(date +%s) - run_started_at ))"
log "Solver process finished in ${run_elapsed}s."

if valid_result "${result_file}" "${RESULT_ROOT}/fast_calib.log"; then
  code=0
  log "Calibration result is valid."
else
  code=1
  if grep -Eq 'Need 4 centers, got|fewer than 4 high-intensity clusters' "${RESULT_ROOT}/fast_calib.log"; then
    log "LiDAR target detection failed: four reflective circle clusters were not found."
  fi
  log "Calibration failed validation; zero/incomplete extrinsics will not be accepted."
fi

cat >"${RESULT_ROOT}/run_metadata.txt" <<EOF
mode=single
dataset_dir=${DATASET_DIR}
package_dir=${PKG_DIR}
solver_exit_code=${solver_code}
exit_code=${code}
created_at=$(date '+%F %T %Z')
EOF

log "Result root: ${RESULT_ROOT}"
exit "${code}"
