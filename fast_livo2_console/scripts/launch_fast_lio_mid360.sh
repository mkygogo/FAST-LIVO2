#!/usr/bin/env bash
set -euo pipefail
cd "${HOME}/fast_livo2_deploy"
docker compose run -T --rm --name jr_lidar_mapping fast-livo2 bash -lc \
  "source /opt/ros/noetic/setup.bash; source /home/jr/fast_livo2_ws/devel/setup.bash; roslaunch fast_lio mapping_mid360.launch rviz:=false"
