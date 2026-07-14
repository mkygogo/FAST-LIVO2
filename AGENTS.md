# AGENTS.md

This repository contains the JR Scanner local touch-console work that was built
around FAST-LIVO2, FAST_LIO, ROS Noetic, Docker, and a Livox Mid360.

Use this file as the first stop for future AI agents and maintainers.

## Project Goal

JR Scanner is intended to run on a small AMD mini PC with a touch screen. The
first production-facing interface is a local browser console at:

```text
http://localhost:8090
```

The console lets the operator start and stop the Mid360 driver, start LiDAR-only
mapping through FAST_LIO, inspect logs/status, record bags, and preview live
point clouds on a touch screen.

## Repository Layout

```text
fast_livo2_console/
  server.py                         Local-only HTTP/WebSocket control server
  ros_point_stream.py               ROS topic to JSON-line point stream bridge
  static/
    index.html                      Touch UI
    style.css                       Touch UI layout and canvas controls
    app.js                          Frontend status, control, and point rendering
    vendor/three.min.js             Local Three.js build for Web RViz-like preview
    map_viewer.html                 Offline PCD/PLY map viewer (served by map_viewer_server.py)
  scripts/
    launch_fast_lio_mid360.sh       Start FAST_LIO LiDAR-only mapping container
    stop_lidar_mapping.sh           Stop FAST_LIO mapping container
    livox_sleep.sh                  Put Mid360 into idle and disable point/IMU send
    livox_wake.sh                   Wake Mid360 for active scanning
    build_livox_power_control.sh    Build Livox SDK power-control helper
  tools/
    livox_power_control.cpp         Livox SDK2 helper for wake/idle control
    ros_image_stream.py             ROS Image to low-FPS JPEG JSON-line bridge
    build_replay_pack.py            Build optional map trajectory replay assets
    map_viewer_server.py            Standalone HTTP server for offline PCD/PLY viewing
    pcd_to_ply.py                   Convert binary PCD with RGB fields to binary PLY
  patches/
    fast_lio_livox_ros_driver2.patch
                                    Patch for mkygogo/FAST_LIO integration
  docs/
    fast_lio_integration.md         How FAST_LIO is integrated
```

## Mini PC Deployment

The current deployed mini PC uses these directories:

```text
/home/jr/fast_livo2_deploy/console   Deployed browser console (server.py, static/)
/home/jr/fast_livo2_deploy/map_viewer Map viewer server and its HTML
/home/jr/fast_livo2_deploy           Docker compose deployment root
/home/jr/fast_livo2_ws               ROS catkin workspace
/home/jr/fast_livo2_data/bags        Rosbag storage
/home/jr/fast_livo2_data/output      Logs and generated output
/home/jr/fast_livo2_data/tools       Runtime helper binaries/scripts
```

Map viewer deployment path (port 18180):

```text
/home/jr/fast_livo2_deploy/map_viewer/map_viewer_server.py
/home/jr/fast_livo2_deploy/map_viewer/map_viewer.html
```

The map viewer process uses `--viewer` pointing to the HTML in the same directory.
When deploying a new `map_viewer.html`, copy to the `map_viewer/` directory, not
`console/static/`.

The map viewer server is deployed separately as `jr-map-viewer.service` on port
18180. Changes to its Python server require restarting that service; HTML changes
are read on each request.

Systemd service:

```bash
sudo systemctl status fast-livo2-console.service
sudo systemctl restart fast-livo2-console.service
```

The service listens on `127.0.0.1:8090` by design. Do not expose it to the LAN or
internet without adding authentication and a clearer security model.

### Console App Shell (Chromium)

Desktop icon **JR扫描仪控制台** must open Chromium App mode, not bare Firefox:

```text
Exec → /home/jr/fast_livo2_deploy/console/scripts/start_console_app.sh
URL  → http://127.0.0.1:8090  (--app, isolated profile)
```

Deploy shell + desktop from this repo:

```bash
scp -i ~/.ssh/jr_fast_livo2_ed25519 \
  fast_livo2_console/scripts/start_console_app.sh \
  jr@192.168.3.59:/home/jr/fast_livo2_deploy/console/scripts/

ssh -i ~/.ssh/jr_fast_livo2_ed25519 jr@192.168.3.59 \
  'chmod +x /home/jr/fast_livo2_deploy/console/scripts/start_console_app.sh'

scp -i ~/.ssh/jr_fast_livo2_ed25519 \
  "fast_livo2_console/JR Scanner.desktop" \
  "jr@192.168.3.59:/home/jr/Desktop/JR扫描仪控制台.desktop"
```

Preferred browser: Chromium/Chrome App mode (`sudo snap install chromium` or
`google-chrome`). If Chromium is missing, the launcher falls back to a dedicated
Firefox profile with session-restore / update nags suppressed (still has a thin
browser chrome). If GNOME marks the desktop untrusted: right-click → Allow
Launching, or `gio set ~/Desktop/JR扫描仪控制台.desktop metadata::trusted true`.
No systemd restart for shell-only changes. Does not auto-start LiDAR/mapping.

## Hardware and Network Assumptions

- LiDAR: Livox Mid360
- LiDAR IP: `192.168.1.151`
- Mini PC LiDAR NIC: `enp1s0`
- Mini PC LiDAR NIC address: `192.168.1.5/24`
- ROS driver: `livox_ros_driver2`
- ROS distro inside container: Noetic
- Default UI is optimized for a small touch screen, not keyboard/mouse use.

## Runtime Containers

Important named containers:

```text
mid360_driver             Mid360 ROS driver
mid360_preview_driver     Preview/test driver
mid360_driver_test        Driver test container
hikrobot_camera           Hikrobot camera ROS driver
jr_lidar_mapping          FAST_LIO LiDAR-only mapping
fast_livo2_mapping        FAST-LIVO2 camera+LiDAR mapping with official PCD save
fast_livo2_bag_record     Bag recording
```

`server.py` only calls whitelisted scripts/actions. Do not add an endpoint that
executes arbitrary shell input from the browser.

## Docker Container Architecture

The deployment uses `docker compose run -T --rm --name <name> fast-livo2 bash -lc
"source ...; roslaunch ..."`.  Key architectural facts:

- **`pid: host`** is set in `docker-compose.yml`.  The container shares the
  host PID namespace.  NEVER use `kill -INT -1` or similar broad signals inside
  the container — it will signal ALL host processes and crash the system.
- **PID 1 is bash**, not roslaunch.  `docker kill --signal=SIGINT <container>`
  only reaches bash, which does NOT forward SIGINT to children in non-interactive
  `-c` mode.
- **Graceful stop** must use `docker exec <name> bash -c "pkill -INT -f
  roslaunch; pkill -INT -f fastlivo_mapping"` to target the actual ROS processes.
  roslaunch then sends SIGTERM to managed nodes, allowing FAST-LIVO2 to run
  `savePCD()` before exiting.
- The `docker_sigint_wait()` function in `server.py` implements this pattern with
  a configurable timeout (default 75s for general, 90s for FAST-LIVO2 mapping).
- Volume mounts: `~/fast_livo2_ws:/home/jr/fast_livo2_ws` and
  `~/fast_livo2_data:/home/jr/fast_livo2_data` — PCD files written inside the
  container appear at the same host path.

## Console Control Semantics

Keep the UI model simple for the touch-screen operator:

- Tab order: `建图` → `数据管理` → `设备概览` → `雷达调试` → `相机调试` →
  `录包` → `日志` → `设置`.
- The `建图` page is the default and primary page. It owns the production
  scanning flow and combines start/finish controls, camera video, live 3D map,
  and final model viewing in one place.
- `开始建图` must start the Mid360 driver, the Hikrobot camera, and
  `fast_livo2_mapping`, then automatically connect `/ws/camera` and
  `/ws/points?mode=mapping`.
- `完成建图` must first stop `fast_livo2_mapping` gracefully so FAST-LIVO2 runs
  its official `savePCD()` path.  The stop uses `docker exec` + `pkill -INT -f
  roslaunch` (NOT `docker kill --signal=SIGINT`, which only reaches bash PID 1).
  After PCD is saved, stop the Hikrobot camera and Mid360 driver, then load the
  saved `all_raw_points.pcd` in the 3D viewer.
- `数据管理` lists saved maps under `fast_livo2_maps/<timestamp>/` (left list,
  right detail). **打开预览** reuses `loadMapFile()` and switches back to the
  `建图` page (prefer `all_raw_points.pcd`, fallback downsampled). API:
  `GET /api/fastlivo/maps` and `GET /api/fastlivo/maps/<id>/<file>`.
- `设备概览` shows host/memory/network status plus **磁盘与数据**: whole-disk
  total/used/free, `fast_livo2_maps` size + scan count, and `bags` size + bag
  count. `/api/status` includes a `storage` object (dir sizes cached ~30s).
- `雷达调试` and `相机调试` are for abnormal-device debugging only.
- `停止全部` should also prefer the graceful FAST-LIVO2 stop path when fusion
  mapping is active (via `docker exec` + `pkill`), then stop any remaining
  helper containers.
- Do not put LiDAR-only FAST_LIO controls back on the `建图` page.

Official FAST-LIVO2 saved maps are copied into:

```text
/home/jr/fast_livo2_data/output/fast_livo2_maps/<timestamp>/
```

The source PCD files are generated by FAST-LIVO2 under:

```text
/home/jr/fast_livo2_ws/src/FAST-LIVO2/Log/pcd/
```

The launch file used for production mapping is:

```text
/home/jr/fast_livo2_ws/src/jr_fastlivo_validation/launch/fast_livo2_saved_mapping.launch
```

It passes `pcd_save_en`, `pcd_save_type`, `pcd_save_interval`, and
`pcd_filter_size` as ROS params and includes a `livox_driver2_to_legacy` bridge
node to convert the Mid360 driver2 topic to the legacy format expected by
FAST-LIVO2.

## FAST_LIO Integration

The LiDAR-only mapping path uses the user's fork:

```text
https://github.com/mkygogo/FAST_LIO
```

The mini PC has this source checked out at:

```text
/home/jr/fast_livo2_ws/src/FAST_LIO
```

Local repository does not vendor the full FAST_LIO source. Instead, it keeps the
required integration patch at:

```text
fast_livo2_console/patches/fast_lio_livox_ros_driver2.patch
```

That patch does two important things:

- Replaces old `livox_ros_driver` references with `livox_ros_driver2`.
- Sets `config/mid360.yaml` `publish.path_en: true` so the browser preview can
  receive `/path` for trajectory display and top-down follow mode.

## Touch Preview Behavior

The preview page is a scanning-focused Web RViz-like view. It is not a generic
RViz plugin replacement, but for the operator's core workflow it should make the
current scan coverage visible without opening RViz.

Current behavior:

- The preview page shows camera video and a Three.js cumulative 3D map together.
- The production `建图` page shows camera video and a Three.js 3D map together;
  there is no separate primary preview page.
- The live map defaults to FPS follow mode using `/aft_mapped_to_init`, with
  `/path` as a fallback for heading/position context. Fullscreen mode exposes
  two touch joysticks for free-fly roaming:
  - Left stick: move (forward follows look direction including pitch; strafe
    is level). Left/right strafe is not inverted.
  - Right stick: look (yaw + pitch).
- Block browser context menus / long-press "Save image" on the 3D viewport and
  joysticks (`contextmenu`, drag, user-select, touch-callout).
- `/cloud_registered` batches are appended to a browser-side cumulative map
  instead of replacing the previous frame.
- Point colors come from `rgb`/`rgba` fields when FAST-LIVO2 publishes RGB
  `PointCloud2`; otherwise the bridge sends a pseudo-color fallback and marks
  the stream as non-RGB.
- The 3D map has two quality modes: mini PC mode keeps a smaller point budget,
  while PC mode allows a larger accumulated point budget for LAN viewing.
- The live 3D preview applies a default Z-axis `-30°` display correction in the
  current Three.js preview coordinate frame for the tilted Mid360 mount. This is
  a visualization-only correction and must not modify FAST-LIVO2 output or saved
  PCD data.
- `/path` and `/aft_mapped_to_init` provide trajectory, pose, follow mode, and
  heading when available. If yaw is missing, the frontend estimates heading from
  recent path points.
- `清空累计` only clears the browser preview cache. It does not affect
  FAST-LIVO2 official PCD saving.

Important: RViz achieves the reference visual effect by displaying
`/cloud_registered` with the `RGB8` color transformer and a long decay time, plus
an Image panel on `/rgb_img`. The web console should preserve those two ideas:
RGB point fields and cumulative display.

Keep UI controls large and touch-friendly. Avoid adding keyboard-only workflows
for primary scanning actions.

## ROS Topics Used

Input and health topics:

```text
/livox/lidar
/livox/imu
/left_camera/image       Hikrobot/raw or alternate image path
/rgb_img                 FAST-LIVO2 RGB image output preferred by RViz-style view
```

Mapping output topics:

```text
/cloud_registered
/path
/aft_mapped_to_init
```

`ros_point_stream.py` intentionally down-samples point clouds before they reach
the browser and should preserve RGB fields when available. Do not log or persist
full point frames in the web server logs. `ros_image_stream.py` sends JPEG frames
through `/ws/camera`; server logs must omit the base64 image payload.

## Build and Verification Commands

Compile the ROS workspace inside the deployment container:

```bash
cd ~/fast_livo2_deploy
docker compose run --rm fast-livo2 bash -lc \
  'source /opt/ros/noetic/setup.bash; cd /home/jr/fast_livo2_ws; catkin_make -DROS_EDITION=ROS1 -DCMAKE_BUILD_TYPE=Release'
```

Check the browser console service:

```bash
sudo systemctl status fast-livo2-console.service
curl -sS http://127.0.0.1:8090/api/status
```

Check JavaScript syntax locally:

```bash
node --check fast_livo2_console/static/app.js
```

Check Python syntax on the mini PC:

```bash
python3 -m py_compile \
  ~/fast_livo2_deploy/console/server.py \
  ~/fast_livo2_data/tools/ros_point_stream.py \
  ~/fast_livo2_data/tools/ros_image_stream.py
```

Check JavaScript syntax after preview edits:

```bash
node --check fast_livo2_console/static/app.js
```

## Operational Safety

- Do not commit SSH passwords, private keys, rosbag data, build outputs, logs, or
  Python `__pycache__`.
- Do not auto-start LiDAR scanning on boot. The console may auto-start, but the
  LiDAR driver and mapping should start only after an operator action.
- **NEVER use `kill -INT -1` or `kill -TERM -1` inside the Docker container.**
  Because `pid: host` is enabled, this kills ALL host processes (desktop, browser,
  systemd services).  Always target specific process names with `pkill -f`.
- `停止全部` should stop relevant containers and call the Livox idle helper.
  This stops point/IMU streaming, but it may not make the physical Mid360 fully
  silent. Full silence requires cutting power to the LiDAR.
- Be careful with Docker container names. The frontend and stop scripts rely on
  stable names.
- Keep the console bound to `127.0.0.1` unless authentication is added.

## GitHub Upload Checklist

Before uploading:

1. Confirm `fast_livo2_console/` is included.
2. Confirm `AGENTS.md` is included.
3. Confirm the FAST_LIO integration patch includes `livox_ros_driver2` changes
   and `path_en: true`.
4. Exclude runtime artifacts such as `__pycache__`, logs, bag files, build
   directories, and helper binaries compiled on the mini PC.
5. If the remote mini PC has newer files, copy them back into this repository
   before committing.

The repository intentionally excludes local agent scratch directories, remote
edit snapshots, scan outputs, screenshots, archives, and historical GS-LIVO
experiment files. The supported Gaussian workflow is the external LOD-3DGS tree
described above.

Keep commits focused on source, launch files, configuration, documentation, and
small static assets. Do not add bags, PCD/PLY outputs, generated replay packs,
build directories, Python caches, or GPU training outputs.

## SSH Deployment from Dev Machine

A dedicated SSH key is configured for non-interactive deployment:

```text
Key: ~/.ssh/jr_fast_livo2_ed25519
Host: jr@192.168.3.59
```

Deploy map viewer:

```bash
scp -i ~/.ssh/jr_fast_livo2_ed25519 fast_livo2_console/static/map_viewer.html jr@192.168.3.59:/home/jr/fast_livo2_deploy/map_viewer/map_viewer.html
```

Deploy console:

```bash
scp -i ~/.ssh/jr_fast_livo2_ed25519 -r fast_livo2_console/ jr@192.168.3.59:/home/jr/fast_livo2_deploy/console/
```

No restart is needed for the map viewer (HTML is re-read on each request).
For the main console, restart the service after deployment:

```bash
ssh -i ~/.ssh/jr_fast_livo2_ed25519 jr@192.168.3.59 sudo systemctl restart fast-livo2-console.service
```

## Saved Scan Data Paths

Primary recorded data on the mini PC:

```text
/home/jr/fast_livo2_data/output/fast_livo2_maps/<YYYYMMDD-HHMMSS>/
  all_raw_points.pcd
  all_downsampled_points.pcd
  metadata.json
  ...

/home/jr/fast_livo2_data/bags/
  mid360-<timestamp>.bag     # console bag-record page
```

Console UI surfaces these under `数据管理` (maps only) and `设备概览` disk
panel (maps + bags sizes).

## Map Viewer Features

The offline map viewer (`map_viewer.html`) provides:

- PCD and PLY file loading with automatic down-sampling to 180k points
- View modes: top-down (俯视), front (前视), roam/FPS (漫游)
- Manual 3-axis alignment (X/Y/Z rotation) to correct tilt in scanned maps
- Alignment angle persisted in browser localStorage
- Real-time FPS counter
- Roam mode: WASD movement, Q/E vertical, mouse-look with pointer lock

## Gaussian Splatting (LOD-3DGS) — Current Path

**GS-LIVO has been abandoned.** Photoreal Gaussian splat training now uses
offline **LOD-3DGS (LetsGo)** on FAST-LIVO2 scan exports (images + poses + LiDAR
point cloud), not online rosbag replay.

Primary code lives outside this repo:

```text
/home/jr/LetsGo/LOD-3DGS/          Training + FAST-LIVO2→dataset conversion
/home/jr/LetsGo/gs_playcanvas_viewer/  PlayCanvas Web viewer (port 18182)
/home/jr/LetsGo/LOD-Web-Viewer/    LOD multi-level web viewer
/home/jr/LetsGo/dataset/           Prepared COLMAP-style scenes
/home/jr/LetsGo/output_gs/         Packaged training run archives
```

Authoritative agent notes: `/home/jr/LetsGo/LOD-3DGS/AGENTS.md` and
`/home/jr/LetsGo/LOD-3DGS/README_CN.md`.

Legacy GS-LIVO scripts/configs still present under `fast_livo2_console/` and
`config/jr_mid360_gs_*.yaml` are historical only — do not use for new runs.

### Pipeline Overview

```text
Mini PC FAST-LIVO2 scan
  → Log images + COLMAP poses + all_raw_points.pcd
  → GPU host: livo_map/convert_to_lod3dgs.py
  → LOD-3DGS train.py (--use_lod recommended)
  → point_cloud.ply / level_*.ply
  → PlayCanvas viewer or LOD-Web-Viewer
```

### Convert FAST-LIVO2 → LOD-3DGS Dataset

```bash
cd /home/jr/LetsGo/LOD-3DGS
python livo_map/convert_to_lod3dgs.py livo_map/<scan_id>
```

The converter pulls from mini PC (`jr@192.168.3.59`, key
`~/.ssh/jr_fast_livo2_ed25519`), renames images, converts PCD→PLY, builds
warpped depths, and runs PotreeConverter for octree. Output:

```text
livo_map/<scan_id>/dataset/
  images/                 00001.png, ...
  depths/                 warpped depth PNGs (optional depth loss)
  sparse/0/
    cameras.txt           PINHOLE fx fy cx cy
    images.txt            COLMAP world-to-camera poses
    points3D.ply          LiDAR RGB point cloud init
  octree/                 Potree v2 hierarchy (LOD mode)
```

### Train

Standard (single level):

```bash
cd /home/jr/LetsGo/LOD-3DGS
python train.py -s <dataset_path> \
  --depths depths --sh_degree 2 --iterations 100000 \
  --data_device cpu -r 1 \
  --densify_until_iter 50000 --opacity_reset_interval 10000
```

LOD (recommended for large scenes):

```bash
python train.py -s <dataset_path> \
  --use_lod --depths depths --sh_degree 2 \
  --iterations 300000 --densification_interval 10000 \
  --scaling_lr 0.0015 --position_lr_init 0.000016 \
  --opacity_reset_interval 300000 --densify_until_iter 200000 \
  --data_device cpu -r 1
```

Training outputs multi-level PLYs under
`<dataset>/3D-Gaussian-Splatting/<run_id>/point_cloud/iteration_*/`.

### View Models

```bash
# PlayCanvas (primary compare/view path)
cd /home/jr/LetsGo/gs_playcanvas_viewer
python3 gs_viewer_server.py --port 18182
# http://<host>:18182 — put PLYs in gs_dataset/; same-name .pcd overlays for measure
```

Optional: `LOD-Web-Viewer` for multi-level LOD web playback.

### Local GPU / LetsGo Layout (this machine)

```text
Host: this GPU workstation (also historically jr@192.168.3.38)
GPU:  NVIDIA RTX-class (e.g. 4070 12 GB)
SSH key for mini PC pull: ~/.ssh/jr_fast_livo2_ed25519 → jr@192.168.3.59
```

```text
/home/jr/LetsGo/
  LOD-3DGS/
    livo_map/
      convert_to_lod3dgs.py     Mini PC → COLMAP+depth+octree
      <scan_id>/dataset/        Per-scan training data
    train.py / render.py
    PotreeConverter/            LOD octree builder
  gs_playcanvas_viewer/
    gs_viewer_server.py         Port 18182
    gs_dataset/                 Models to load in browser
  LOD-Web-Viewer/               LOD hierarchical web viewer
  dataset/                      Shared prepared scenes (JR_Mid360, ...)
  output_gs/                    Exported run tarballs / checkpoints
```

### Viewer Ports

```text
18180   Map viewer (PCD/PLY point cloud) — mini PC / deploy
18182   PlayCanvas Gaussian viewer — primary for LOD-3DGS PLY
```

### Why LOD-3DGS Replaced GS-LIVO

| | GS-LIVO (abandoned) | LOD-3DGS (current) |
|--|---------------------|--------------------|
| Mode | Online ROS bag replay | Offline batch training |
| Input | `/left_camera/image` + lidar + imu bag | images + COLMAP poses + LiDAR PLY |
| Init | Incremental octree spawn | Full LiDAR cloud + optional depth loss |
| LOD | No | Multi-level Potree octree |
| Result quality on JR Mid360 indoor | Fog / point-cloud / streaks after long tuning | Trainable path already producing usable PLYs |

Do not restart GS-LIVO config experiments (`gs_scaling_lr`, paper_v* YAML, etc.).
New Gaussian work should go through `~/LetsGo/LOD-3DGS`.
