#!/usr/bin/env bash
set -euo pipefail
cd "${HOME}/fast_livo2_deploy"
mkdir -p "${HOME}/fast_livo2_data/tools"
cp console/tools/livox_power_control.cpp "${HOME}/fast_livo2_data/tools/livox_power_control.cpp"
docker compose run -T --rm fast-livo2 bash -lc \
  "g++ -std=c++14 -O2 /home/jr/fast_livo2_data/tools/livox_power_control.cpp -o /home/jr/fast_livo2_data/tools/livox_power_control -I/usr/local/include -L/usr/local/lib -llivox_lidar_sdk_shared -lpthread -Wl,-rpath,/usr/local/lib"
