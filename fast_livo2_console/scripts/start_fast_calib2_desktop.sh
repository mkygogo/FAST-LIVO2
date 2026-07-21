#!/usr/bin/env bash
set -u

DEPLOY_DIR="${FAST_LIVO2_DEPLOY_DIR:-${HOME}/fast_livo2_deploy}"
CONSOLE_DIR="${DEPLOY_DIR}/console"
SCRIPT_DIR="${CONSOLE_DIR}/scripts"
DATA_ROOT="${FAST_CALIB2_DATA_ROOT:-${HOME}/fast_livo2_data/calib/fast_calib2_mv_cs050}"
RECORD_SCRIPT="${SCRIPT_DIR}/record_fast_calib2_dataset.sh"
RUN_SCRIPT="${SCRIPT_DIR}/run_fast_calib2_calibration.sh"
SINGLE_RUN_SCRIPT="${SCRIPT_DIR}/run_fast_calib2_single.sh"
REVIEW_SCRIPT="${SCRIPT_DIR}/review_fast_calib2_result.sh"
DEVICE_SCRIPT="${SCRIPT_DIR}/fast_calib2_devices.sh"
LOG_DIR="${DATA_ROOT}/logs"

export FAST_LIVO2_DEPLOY_DIR="${DEPLOY_DIR}"
export FAST_CALIB2_DATA_ROOT="${DATA_ROOT}"
export FAST_CALIB2_INTRINSICS="${DATA_ROOT}/intrinsics/jr_mvs_fast_calib2_intrinsics.yaml"

show_error() {
  zenity --error --title="FAST-Calib2雷达相机标定" --width=520 --text="$1"
}

require_file() {
  if [[ ! -f "$1" ]]; then
    show_error "缺少运行脚本：\n$1"
    return 1
  fi
}

run_terminal() {
  local title="$1"
  local log_file="$2"
  shift 2
  gnome-terminal --wait --title="${title}" -- /bin/bash -lc '
    log_file="$1"
    shift
    mkdir -p "$(dirname "${log_file}")"
    set +e
    "$@" 2>&1 | tee "${log_file}"
    code=${PIPESTATUS[0]}
    echo
    if [[ ${code} -eq 0 ]]; then
      echo "操作完成。"
    else
      echo "操作失败，退出码：${code}"
    fi
    echo "日志：${log_file}"
    echo "按 Enter 关闭窗口..."
    read -r
    exit "${code}"
  ' _ "${log_file}" "$@"
}

show_help() {
  zenity --info --title="FAST-Calib2雷达相机标定" --width=660 --height=430 --text="$(cat <<EOF
推荐流程：

1. 将相机接回小主机，并保持镜头焦距不变。
2. 点击“一键启动标定设备”，工具会启动 Mid360 和海康外触发相机。
3. 使用“检查设备状态”确认以下话题都有实时数据：
   /livox/lidar
   /livox/imu
   /left_camera/image
4. 从不同位置录制至少 3 组静止标定数据。
5. 先对最新一组执行“单组标定”。
6. 单组均正常后执行“多组联合标定”。
7. 使用“查看最新结果”检查输出，完成后停止标定设备。

新相机数据目录：
${DATA_ROOT}

注意：该目录与旧 MV-CA013-A0UC 数据隔离，不要混用。
EOF
)"
}

mkdir -p "${DATA_ROOT}/datasets" "${DATA_ROOT}/results" "${DATA_ROOT}/intrinsics" "${LOG_DIR}"

while true; do
  action="$(zenity --list \
    --title="FAST-Calib2雷达相机标定" \
    --width=720 --height=460 \
    --column="操作" --column="说明" \
    "查看操作说明" "显示标定顺序和注意事项" \
    "一键启动标定设备" "启动Mid360和海康Line0外触发相机，并等待实时数据" \
    "检查设备状态" "检查USB、网络、容器及三路ROS话题" \
    "录制一组标定数据" "打开实时取景和标记检查，确认后采集图像、雷达和IMU 3秒" \
    "单组标定（最新数据）" "检查最新一组数据是否可正常求解" \
    "多组联合标定" "联合按修改时间选择的最新3组完整数据" \
    "查看最新结果" "在终端显示最新标定结果和日志摘要" \
    "打开数据目录" "打开新相机 FAST-Calib2 数据目录" \
    "停止标定设备" "停止外触发相机和Mid360驱动，并让雷达进入空闲" \
    "退出" "关闭本工具" 2>/dev/null)" || exit 0

  case "${action}" in
    "查看操作说明")
      show_help
      ;;
    "一键启动标定设备")
      require_file "${DEVICE_SCRIPT}" || continue
      run_terminal "FAST-Calib2 - 启动设备" \
        "${LOG_DIR}/devices-start-$(date +%Y%m%d-%H%M%S).log" \
        "${DEVICE_SCRIPT}" start
      ;;
    "检查设备状态")
      require_file "${DEVICE_SCRIPT}" || continue
      run_terminal "FAST-Calib2 - 设备状态" \
        "${LOG_DIR}/devices-status-$(date +%Y%m%d-%H%M%S).log" \
        "${DEVICE_SCRIPT}" status
      ;;
    "录制一组标定数据")
      require_file "${DEVICE_SCRIPT}" || continue
      require_file "${RECORD_SCRIPT}" || continue
      record_log="${LOG_DIR}/record-$(date +%Y%m%d-%H%M%S).log"
      /bin/bash -c '"$1" ensure && "$2"' _ \
        "${DEVICE_SCRIPT}" "${RECORD_SCRIPT}" >"${record_log}" 2>&1
      code=$?
      if [[ "${code}" -ne 0 && "${code}" -ne 2 ]]; then
        show_error "录制未完成。请检查相机、标定板取景和日志：\n${record_log}"
      fi
      ;;
    "单组标定（最新数据）")
      require_file "${SINGLE_RUN_SCRIPT}" || continue
      run_terminal "FAST-Calib2 - 单组标定" \
        "${LOG_DIR}/single-$(date +%Y%m%d-%H%M%S).log" \
        "${SINGLE_RUN_SCRIPT}"
      ;;
    "多组联合标定")
      require_file "${RUN_SCRIPT}" || continue
      if zenity --question --title="FAST-Calib2多组联合标定" --width=560 \
        --text="将使用按修改时间选择的最新3组完整数据，并先逐组验证四圆检测。请确认它们是正面、偏左、偏右三组且录制时标定板完全静止。是否继续？"; then
        run_terminal "FAST-Calib2 - 多组联合标定" \
          "${LOG_DIR}/multi-$(date +%Y%m%d-%H%M%S).log" \
          "${RUN_SCRIPT}" --mode multi --multi-root "${DATA_ROOT}/datasets"
      fi
      ;;
    "查看最新结果")
      require_file "${REVIEW_SCRIPT}" || continue
      run_terminal "FAST-Calib2 - 最新结果" \
        "${LOG_DIR}/review-$(date +%Y%m%d-%H%M%S).log" \
        "${REVIEW_SCRIPT}"
      ;;
    "打开数据目录")
      xdg-open "${DATA_ROOT}" >/dev/null 2>&1 &
      ;;
    "停止标定设备")
      require_file "${DEVICE_SCRIPT}" || continue
      run_terminal "FAST-Calib2 - 停止设备" \
        "${LOG_DIR}/devices-stop-$(date +%Y%m%d-%H%M%S).log" \
        "${DEVICE_SCRIPT}" stop
      ;;
    "退出")
      exit 0
      ;;
  esac
done
