#!/usr/bin/env bash
set -euo pipefail

DATA_ROOT="${FAST_CALIB2_DATA_ROOT:-${HOME}/fast_livo2_data/calib/fast_calib2}"
USE_DOCKER="${FAST_CALIB2_USE_DOCKER:-1}"
DEPLOY_DIR="${FAST_LIVO2_DEPLOY_DIR:-${HOME}/fast_livo2_deploy}"
TARGET="${1:-}"

usage() {
  cat <<EOF
Usage: $0 [DATASET_OR_RESULT_DIR]

Prints a compact summary of FAST-Calib2 input data and generated results.
Defaults to the newest result directory, then newest dataset directory.
EOF
}

if [[ "${TARGET}" == "-h" || "${TARGET}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ -z "${TARGET}" ]]; then
  TARGET="$(find "${DATA_ROOT}/results" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' 2>/dev/null |
    sort -nr | head -1 | cut -d' ' -f2- || true)"
  if [[ -z "${TARGET}" ]]; then
    TARGET="$(find "${DATA_ROOT}/datasets" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' 2>/dev/null |
      sort -nr | head -1 | cut -d' ' -f2- || true)"
  fi
fi
[[ -n "${TARGET}" && -d "${TARGET}" ]] || { echo "No dataset/result directory found." >&2; exit 1; }

ros_exec() {
  local cmd="$1"
  if [[ "${USE_DOCKER}" == "1" && -d "${DEPLOY_DIR}" ]]; then
    cd "${DEPLOY_DIR}"
    docker compose run --rm fast-livo2 bash -lc "source /opt/ros/noetic/setup.bash; ${cmd}"
  else
    bash -lc "source /opt/ros/noetic/setup.bash 2>/dev/null || true; ${cmd}"
  fi
}

echo "=== FAST-Calib2 Review ==="
echo "target: ${TARGET}"
echo

echo "--- files ---"
find "${TARGET}" -maxdepth 3 -type f | sort
echo

echo "--- metadata ---"
for file in "${TARGET}/metadata.json" "${TARGET}/run_metadata.txt"; do
  [[ -f "${file}" ]] && { echo "### ${file}"; cat "${file}"; echo; }
done

echo "--- intrinsics/config ---"
for file in \
  "${TARGET}/camera_intrinsics_fast_calib2.yaml" \
  "${TARGET}/fast_calib2_intrinsics.yaml" \
  "${TARGET}/qr_params.used.yaml"; do
  [[ -f "${file}" ]] && { echo "### ${file}"; sed -n '1,120p' "${file}"; echo; }
done

bag="$(find "${TARGET}" -maxdepth 3 -name '*.bag' -type f | sort | head -1 || true)"
if [[ -n "${bag}" ]]; then
  echo "--- rosbag info ---"
  ros_exec "rosbag info '${bag}'" || true
  echo
fi

echo "--- likely result snippets ---"
find "${TARGET}" -maxdepth 5 -type f \( -name '*.yaml' -o -name '*.txt' -o -name '*.log' \) | sort | while read -r file; do
  case "$(basename "${file}")" in
    fast_calib.log)
      echo "### ${file} tail"
      tail -80 "${file}"
      ;;
    *extrinsic*|*result*|*transform*|*calib*)
      echo "### ${file}"
      sed -n '1,160p' "${file}" || true
      ;;
  esac
done
