#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="${FAST_LIVO2_DEPLOY_DIR:-${HOME}/fast_livo2_deploy}"
WS="${FAST_CALIB2_WS:-${HOME}/fast_livo2_ws}"
DATA_ROOT="${FAST_CALIB2_DATA_ROOT:-${HOME}/fast_livo2_data/calib/fast_calib2}"
USE_DOCKER="${FAST_CALIB2_USE_DOCKER:-1}"
LAUNCH_TIMEOUT_SEC="${FAST_CALIB2_LAUNCH_TIMEOUT_SEC:-45}"
ROS_CONTAINER="${FAST_CALIB2_ROS_CONTAINER:-jr_hik_trig_view}"
SINGLE_RUN_SCRIPT="${FAST_CALIB2_SINGLE_RUN_SCRIPT:-$(dirname "$0")/run_fast_calib2_single.sh}"
MULTI_SCENE_COUNT="${FAST_CALIB2_MULTI_SCENE_COUNT:-3}"
MODE="single"
DATASET_DIR=""
MULTI_ROOT=""
LAUNCH_FILE=""

usage() {
  cat <<EOF
Usage:
  $0 --dataset-dir DIR [--launch FILE]
  $0 --multi-root DIR --mode multi [--launch FILE]

Defaults:
  single launch: calib.launch
  multi launch:  multi_calib.launch

For multi mode, this script selects the latest three complete datasets by
filesystem modification time, validates each with the one-shot single-scene
runner, then runs the one-shot multi_fast_calib executable.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dataset-dir)
      DATASET_DIR="${2:?missing dataset dir}"
      shift 2
      ;;
    --multi-root)
      MULTI_ROOT="${2:?missing multi root}"
      shift 2
      ;;
    --mode)
      MODE="${2:?missing mode}"
      shift 2
      ;;
    --launch)
      LAUNCH_FILE="${2:?missing launch file}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

log() {
  echo "[FAST-Calib2 run] $*"
}

die() {
  echo "[FAST-Calib2 run] ERROR: $*" >&2
  exit 1
}

container_running() {
  docker ps --format '{{.Names}}' | grep -qx "$1"
}

valid_center_record() {
  local record="$1"
  python3 - "${record}" <<'PY'
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
if not path.is_file():
    raise SystemExit(1)
lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
lidar = next((line for line in lines if line.startswith("lidar_centers:")), "")
camera = next((line for line in lines if line.startswith("qr_centers:")), "")
if len(re.findall(r"\{[^}]+\}", lidar)) != 4:
    raise SystemExit(1)
if len(re.findall(r"\{[^}]+\}", camera)) != 4:
    raise SystemExit(1)
PY
}

ros_exec() {
  local cmd="$1"
  if [[ "${USE_DOCKER}" == "1" ]]; then
    [[ -d "${DEPLOY_DIR}" ]] || die "Docker deploy dir not found: ${DEPLOY_DIR}"
    cd "${DEPLOY_DIR}"
    docker compose run --rm fast-livo2 bash -lc "source /opt/ros/noetic/setup.bash; source '${WS}/devel/setup.bash'; ${cmd}"
  else
    bash -lc "source /opt/ros/noetic/setup.bash; source '${WS}/devel/setup.bash'; ${cmd}"
  fi
}

ros_exec_timeout() {
  local seconds="$1"
  local cmd="$2"
  if [[ "${USE_DOCKER}" == "1" ]]; then
    [[ -d "${DEPLOY_DIR}" ]] || die "Docker deploy dir not found: ${DEPLOY_DIR}"
    cd "${DEPLOY_DIR}"
    timeout "${seconds}" docker compose run --rm fast-livo2 bash -lc "source /opt/ros/noetic/setup.bash; source '${WS}/devel/setup.bash'; ${cmd}"
  else
    timeout "${seconds}" bash -lc "source /opt/ros/noetic/setup.bash; source '${WS}/devel/setup.bash'; ${cmd}"
  fi
}

valid_dataset() {
  local dir="$1"
  [[ -f "${dir}/image.png" && -f "${dir}/scene.bag" && \
     -f "${dir}/camera_intrinsics_fast_calib2.yaml" ]] || return 1

  # New recordings contain an immediate LiDAR four-circle validation result.
  # Keep legacy datasets without this field compatible, but never select a
  # dataset that the recording UI explicitly marked invalid.
  [[ ! -f "${dir}/metadata.json" ]] || python3 - "${dir}/metadata.json" <<'PY'
import json
import sys

try:
    metadata = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)

validation = metadata.get("lidar_validation")
if not isinstance(validation, dict):
    raise SystemExit(0)

valid = (
    metadata.get("status") == "complete"
    and validation.get("status") == "passed"
    and validation.get("center_count") == 4
)
raise SystemExit(0 if valid else 1)
PY
}

# Keep the legacy combined entry point compatible, but route single-scene work
# through the validated one-shot runner. The old roslaunch path stays below only
# for historical readability and is unreachable after this delegation.
if [[ "${MODE}" == "single" ]]; then
  [[ -x "${SINGLE_RUN_SCRIPT}" ]] || die "single-scene runner not found: ${SINGLE_RUN_SCRIPT}"
  if [[ -n "${DATASET_DIR}" ]]; then
    exec env \
      FAST_CALIB2_DATA_ROOT="${DATA_ROOT}" \
      FAST_CALIB2_ROS_CONTAINER="${ROS_CONTAINER}" \
      FAST_CALIB2_LAUNCH_TIMEOUT_SEC="${LAUNCH_TIMEOUT_SEC}" \
      "${SINGLE_RUN_SCRIPT}" --dataset-dir "${DATASET_DIR}"
  fi
  exec env \
    FAST_CALIB2_DATA_ROOT="${DATA_ROOT}" \
    FAST_CALIB2_ROS_CONTAINER="${ROS_CONTAINER}" \
    FAST_CALIB2_LAUNCH_TIMEOUT_SEC="${LAUNCH_TIMEOUT_SEC}" \
    "${SINGLE_RUN_SCRIPT}"
fi

update_config() {
  local config="$1"
  local intrinsics="$2"
  local image_path="$3"
  local bag_path="$4"
  local multi_root="$5"
  local output_path="$6"
  python3 - "${config}" "${intrinsics}" "${image_path}" "${bag_path}" "${multi_root}" "${output_path}" <<'PY'
import pathlib
import re
import sys

config = pathlib.Path(sys.argv[1])
intrinsics = pathlib.Path(sys.argv[2])
image_path = sys.argv[3]
bag_path = sys.argv[4]
multi_root = sys.argv[5]
result_root = sys.argv[6]

vals = {}
for line in intrinsics.read_text(encoding="utf-8").splitlines():
    if ":" not in line or line.lstrip().startswith("#"):
        continue
    key, value = line.split(":", 1)
    key = key.strip()
    value = value.strip()
    if key in {"fx", "fy", "cx", "cy", "k1", "k2", "p1", "p2", "k3"}:
        vals[key] = value

path_vals = {
    "bag_path": bag_path,
    "bagfile": bag_path,
    "bag_file": bag_path,
    "img_path": image_path,
    "image_path": image_path,
    "pic_path": image_path,
    "picture_path": image_path,
    "data_path": multi_root,
    "dataset_path": multi_root,
    "root_path": multi_root,
    "output_path": result_root,
}

changed = set()
lines = []
for line in config.read_text(encoding="utf-8").splitlines():
    match = re.match(r"^(\s*)([A-Za-z0-9_./-]+)(\s*:\s*)(.*)$", line)
    if not match:
        lines.append(line)
        continue
    indent, key, sep, old = match.groups()
    clean = key.split("/")[-1]
    if clean in vals:
        lines.append(f"{indent}{key}{sep}{vals[clean]}")
        changed.add(clean)
    elif clean in path_vals and path_vals[clean]:
        lines.append(f'{indent}{key}{sep}"{path_vals[clean]}"')
        changed.add(clean)
    else:
        lines.append(line)

config.write_text("\n".join(lines) + "\n", encoding="utf-8")
required = {"fx", "fy", "cx", "cy", "k1", "k2", "p1", "p2"}
missing = sorted(required - changed)
if missing:
    print("WARNING: these intrinsic keys were not found in qr_params.yaml:", ",".join(missing))
PY
}

if [[ "${MODE}" == "single" ]]; then
  [[ -n "${DATASET_DIR}" ]] || DATASET_DIR="$(find "${DATA_ROOT}/datasets" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort | tail -1 || true)"
  [[ -n "${DATASET_DIR}" && -d "${DATASET_DIR}" ]] || die "dataset dir not found"
  valid_dataset "${DATASET_DIR}" || die "dataset is incomplete: ${DATASET_DIR}"
  LAUNCH_FILE="${LAUNCH_FILE:-calib.launch}"
elif [[ "${MODE}" == "multi" ]]; then
  [[ -n "${MULTI_ROOT}" ]] || MULTI_ROOT="${DATA_ROOT}/datasets"
  [[ -d "${MULTI_ROOT}" ]] || die "multi root not found: ${MULTI_ROOT}"
  LAUNCH_FILE="${LAUNCH_FILE:-multi_calib.launch}"
else
  die "mode must be single or multi"
fi

RESULT_ROOT="${DATA_ROOT}/results/$(date +%Y%m%d-%H%M%S)-${MODE}"
mkdir -p "${RESULT_ROOT}"

log "Locating FAST-Calib2 package..."
if [[ "${MODE}" == "multi" ]]; then
  container_running "${ROS_CONTAINER}" || \
    die "ROS device container is not running: ${ROS_CONTAINER}. Start calibration devices first."
  [[ -x "${SINGLE_RUN_SCRIPT}" ]] || die "single-scene runner not found: ${SINGLE_RUN_SCRIPT}"
  PKG_DIR="$(docker exec "${ROS_CONTAINER}" bash -lc \
    "source /opt/ros/noetic/setup.bash; source '${WS}/devel/setup.bash'; rospack find fast_calib" | tail -1 | tr -d '\r')"
else
  PKG_DIR="$(ros_exec "rospack find fast_calib" | tail -1 | tr -d '\r')"
fi
[[ -n "${PKG_DIR}" ]] || die "rospack could not find package fast_calib"
CONFIG="${PKG_DIR}/config/qr_params.yaml"
[[ -f "${CONFIG}" ]] || die "FAST-Calib2 config not found: ${CONFIG}"
cp -f "${CONFIG}" "${RESULT_ROOT}/qr_params.before.yaml"

START_MARK="${RESULT_ROOT}/start.mark"
touch "${START_MARK}"

if [[ "${MODE}" == "single" ]]; then
  IMAGE_PATH="${DATASET_DIR}/image.png"
  BAG_PATH="${DATASET_DIR}/scene.bag"
  INTRINSICS="${DATASET_DIR}/camera_intrinsics_fast_calib2.yaml"

  log "Updating ${CONFIG}"
  update_config "${CONFIG}" "${INTRINSICS}" "${IMAGE_PATH}" "${BAG_PATH}" "" "${RESULT_ROOT}"
  cp -f "${CONFIG}" "${RESULT_ROOT}/qr_params.used.yaml"

  log "Launching fast_calib/${LAUNCH_FILE}"
  set +e
  ros_exec_timeout "${LAUNCH_TIMEOUT_SEC}" "roslaunch fast_calib '${LAUNCH_FILE}' rviz:=false" 2>&1 | tee "${RESULT_ROOT}/fast_calib.log"
  code=${PIPESTATUS[0]}
  set -e
  if [[ -f "${RESULT_ROOT}/single_calib_result.txt" ]]; then
    code=0
  else
    code=1
  fi
else
  [[ "${MULTI_SCENE_COUNT}" =~ ^[0-9]+$ && "${MULTI_SCENE_COUNT}" -ge 3 ]] || \
    die "FAST_CALIB2_MULTI_SCENE_COUNT must be an integer >= 3"
  mapfile -t DATASETS < <(
    find "${MULTI_ROOT}" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' |
      sort -nr | cut -d' ' -f2-
  )
  VALID_DATASETS=()
  for dir in "${DATASETS[@]}"; do
    if valid_dataset "${dir}"; then
      VALID_DATASETS+=("${dir}")
      [[ ${#VALID_DATASETS[@]} -ge ${MULTI_SCENE_COUNT} ]] && break
    elif [[ -f "${dir}/metadata.json" ]]; then
      log "Skipping dataset not eligible for calibration: $(basename "${dir}")"
    fi
  done
  [[ ${#VALID_DATASETS[@]} -eq ${MULTI_SCENE_COUNT} ]] || \
    die "multi mode needs ${MULTI_SCENE_COUNT} complete recent datasets, got ${#VALID_DATASETS[@]}"

  # Extraction and summary order should be chronological even though selection
  # above starts with the newest directory.
  SELECTED_DATASETS=()
  for ((i=${#VALID_DATASETS[@]} - 1; i >= 0; --i)); do
    SELECTED_DATASETS+=("${VALID_DATASETS[i]}")
  done
  log "Selected the latest ${#SELECTED_DATASETS[@]} complete datasets:"
  for dir in "${SELECTED_DATASETS[@]}"; do
    log "  $(basename "${dir}")"
  done

  SUMMARY="${RESULT_ROOT}/circle_center_record.txt"
  : >"${SUMMARY}"
  EXTRACT_ROOT="${RESULT_ROOT}/single_extract"
  mkdir -p "${EXTRACT_ROOT}"

  code=0
  first_intrinsics="${SELECTED_DATASETS[0]}/camera_intrinsics_fast_calib2.yaml"
  failed_datasets=()
  for dir in "${SELECTED_DATASETS[@]}"; do
    name="$(basename "${dir}")"
    out_dir="${EXTRACT_ROOT}/${name}"
    mkdir -p "${out_dir}"
    log "Validating and extracting centers from ${name}"
    set +e
    FAST_CALIB2_DATA_ROOT="${DATA_ROOT}" \
    FAST_CALIB2_ROS_CONTAINER="${ROS_CONTAINER}" \
    FAST_CALIB2_LAUNCH_TIMEOUT_SEC="${LAUNCH_TIMEOUT_SEC}" \
      "${SINGLE_RUN_SCRIPT}" --dataset-dir "${dir}" 2>&1 | tee "${out_dir}/single_runner.log"
    one_code=${PIPESTATUS[0]}
    set -e
    single_result="$(sed -n 's/^\[FAST-Calib2 single\] Result root: //p' "${out_dir}/single_runner.log" | tail -1)"
    if [[ ${one_code} -ne 0 || -z "${single_result}" || ! -d "${single_result}" || \
          ! -s "${single_result}/circle_center_record.txt" ]] || \
       ! valid_center_record "${single_result}/circle_center_record.txt"; then
      log "Center extraction failed for ${name}; multi calibration will not use older fallback data"
      failed_datasets+=("${name}")
      code=1
      continue
    fi
    cp -f "${single_result}/circle_center_record.txt" "${out_dir}/circle_center_record.txt"
    cp -f "${single_result}/single_calib_result.txt" "${out_dir}/single_calib_result.txt"
    cp -f "${single_result}/fast_calib.log" "${out_dir}/fast_calib.log"
    cat "${out_dir}/circle_center_record.txt" >>"${SUMMARY}"
  done

  if [[ ${#failed_datasets[@]} -gt 0 ]]; then
    die "latest scene extraction failed: ${failed_datasets[*]}"
  fi

  block_count="$(grep -c '^time:' "${SUMMARY}" || true)"
  [[ "${block_count}" -eq "${MULTI_SCENE_COUNT}" ]] || \
    die "expected ${MULTI_SCENE_COUNT} center blocks, got ${block_count}; cannot run multi calibration"

  log "Running multi-scene solve with ${block_count} center blocks"
  update_config "${CONFIG}" "${first_intrinsics}" "" "" "${MULTI_ROOT}" "${RESULT_ROOT}"
  cp -f "${CONFIG}" "${RESULT_ROOT}/qr_params.used.yaml"
  set +e
  docker exec "${ROS_CONTAINER}" bash -lc "
source /opt/ros/noetic/setup.bash
source '${WS}/devel/setup.bash'
rosparam load '${CONFIG}'
timeout --foreground --kill-after=5s '${LAUNCH_TIMEOUT_SEC}s' \\
  rosrun fast_calib multi_fast_calib __name:=multi_fast_calib
" 2>&1 | tee "${RESULT_ROOT}/fast_calib.log"
  multi_code=${PIPESTATUS[0]}
  set -e
  if [[ -s "${RESULT_ROOT}/multi_calib_result.txt" ]]; then
    code=0
  else
    log "Multi solve ended with code ${multi_code} and did not produce multi_calib_result.txt"
    code=1
  fi
fi

log "Collecting generated files newer than run start..."
find "${PKG_DIR}" -type f -newer "${START_MARK}" \
  ! -path "*/build/*" ! -path "*/devel/*" \
  -print >"${RESULT_ROOT}/generated_files.txt" || true
while IFS= read -r file; do
  [[ -f "${file}" ]] || continue
  rel="${file#${PKG_DIR}/}"
  mkdir -p "${RESULT_ROOT}/generated/$(dirname "${rel}")"
  cp -f "${file}" "${RESULT_ROOT}/generated/${rel}" || true
done <"${RESULT_ROOT}/generated_files.txt"

cat >"${RESULT_ROOT}/run_metadata.txt" <<EOF
mode=${MODE}
dataset_dir=${DATASET_DIR}
multi_root=${MULTI_ROOT}
package_dir=${PKG_DIR}
launch_file=${LAUNCH_FILE}
exit_code=${code}
created_at=$(date '+%F %T %Z')
EOF

log "Result root: ${RESULT_ROOT}"
exit "${code}"
