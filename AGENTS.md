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
    map_viewer.html                 Offline PCD/PLY map viewer (served by map_viewer_server.py)
  scripts/
    launch_fast_lio_mid360.sh       Start FAST_LIO LiDAR-only mapping container
    stop_lidar_mapping.sh           Stop FAST_LIO mapping container
    livox_sleep.sh                  Put Mid360 into idle and disable point/IMU send
    livox_wake.sh                   Wake Mid360 for active scanning
    build_livox_power_control.sh    Build Livox SDK power-control helper
  tools/
    livox_power_control.cpp         Livox SDK2 helper for wake/idle control
    map_viewer_server.py            Standalone HTTP server for offline PCD/PLY viewing
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

Systemd service:

```bash
sudo systemctl status fast-livo2-console.service
sudo systemctl restart fast-livo2-console.service
```

The service listens on `127.0.0.1:8090` by design. Do not expose it to the LAN or
internet without adding authentication and a clearer security model.

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
jr_lidar_mapping          FAST_LIO LiDAR-only mapping
fast_livo2_mapping        Reserved FAST-LIVO2 fusion mapping
fast_livo2_bag_record     Bag recording
```

`server.py` only calls whitelisted scripts/actions. Do not add an endpoint that
executes arbitrary shell input from the browser.

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

The preview page is intentionally Canvas-based, not full RViz. It is designed to
stay usable on the small touch screen.

Current behavior:

- Default mapping preview is top-down follow mode.
- Right-side overlay buttons provide `+` and `-` zoom controls.
- A top-right icon button toggles fullscreen mode.
- View modes: top, front, side, free 3D.
- Top/front/side single-finger drag pans the scene.
- Free 3D single-finger drag rotates the scene.
- Double tap recenters to the current pose.
- `/path` and `/aft_mapped_to_init` provide trajectory, pose, and yaw when
  available. If yaw is missing, the frontend estimates heading from recent path
  points.

Keep UI controls large and touch-friendly. Avoid adding keyboard-only workflows
for primary scanning actions.

## ROS Topics Used

Input and health topics:

```text
/livox/lidar
/livox/imu
/left_camera/image       Future camera path
/rgb_img                 Future/alternate image path
```

Mapping output topics:

```text
/cloud_registered
/path
/aft_mapped_to_init
```

`ros_point_stream.py` intentionally down-samples point clouds before they reach
the browser. Do not log or persist full point frames in the web server logs.

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
  ~/fast_livo2_data/tools/ros_point_stream.py
```

## Operational Safety

- Do not commit SSH passwords, private keys, rosbag data, build outputs, logs, or
  Python `__pycache__`.
- Do not auto-start LiDAR scanning on boot. The console may auto-start, but the
  LiDAR driver and mapping should start only after an operator action.
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

## Map Viewer Features

The offline map viewer (`map_viewer.html`) provides:

- PCD and PLY file loading with automatic down-sampling to 180k points
- View modes: top-down (俯视), front (前视), roam/FPS (漫游)
- Manual 3-axis alignment (X/Y/Z rotation) to correct tilt in scanned maps
- Alignment angle persisted in browser localStorage
- Real-time FPS counter
- Roam mode: WASD movement, Q/E vertical, mouse-look with pointer lock

