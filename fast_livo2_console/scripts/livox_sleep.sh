#!/usr/bin/env bash
set -euo pipefail
cd "${HOME}/fast_livo2_deploy"
docker compose run -T --rm fast-livo2 bash -lc \
  "/home/jr/fast_livo2_data/tools/livox_power_control sleep /home/jr/fast_livo2_ws/src/livox_ros_driver2/config/MID360_config.json 192.168.1.5"
