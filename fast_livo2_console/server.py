#!/usr/bin/env python3
import asyncio
import base64
import hashlib
import json
import os
import pathlib
import re
import signal
import shutil
import subprocess
import time
import urllib.parse


ROOT = pathlib.Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
HOME = pathlib.Path.home()
DEPLOY_DIR = HOME / "fast_livo2_deploy"
DATA_DIR = HOME / "fast_livo2_data"
OUTPUT_DIR = DATA_DIR / "output"
TOOLS_DIR = DATA_DIR / "tools"
BAGS_DIR = DATA_DIR / "bags"
LOG_DIR = OUTPUT_DIR / "console_logs"
FASTLIVO_MAP_ROOT = OUTPUT_DIR / "fast_livo2_maps"
GS_DATASET_ROOT = OUTPUT_DIR / "gs_livo_datasets"
FASTLIVO_ROOT = HOME / "fast_livo2_ws" / "src" / "FAST-LIVO2"
FASTLIVO_LOG_DIR = FASTLIVO_ROOT / "Log"
FASTLIVO_PCD_DIR = HOME / "fast_livo2_ws" / "src" / "FAST-LIVO2" / "Log" / "pcd"
FASTLIVO_CONFIG_DIR = FASTLIVO_ROOT / "config"
FASTLIVO_ACTIVE_SCAN = OUTPUT_DIR / "active_fast_livo2_scan.json"
GS_SYNC_TARGET = os.environ.get("GS_LIVO_SYNC_TARGET", "jr@192.168.3.38:~/fast_livo2/gs_livo_datasets/")
HOST = "127.0.0.1"
PORT = int(os.environ.get("FAST_LIVO2_CONSOLE_PORT", "8090"))


CONTAINERS = {
    "lidar": ["mid360_driver", "mid360_preview_driver", "mid360_driver_test"],
    "camera": ["hikrobot_camera"],
    "lio": ["jr_lidar_mapping"],
    "fusion": ["fast_livo2_mapping"],
    "bag": ["fast_livo2_bag_record"],
    "gs_bag": ["fast_livo2_gs_raw_bag_record"],
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def ensure_dirs():
    for path in (OUTPUT_DIR, TOOLS_DIR, BAGS_DIR, LOG_DIR, FASTLIVO_MAP_ROOT, GS_DATASET_ROOT):
        path.mkdir(parents=True, exist_ok=True)


def now_name(prefix):
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}.log"


def log_path(prefix):
    ensure_dirs()
    return LOG_DIR / now_name(prefix)


def run_cmd(args, timeout=12, cwd=None):
    started = time.time()
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd or HOME),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "duration": round(time.time() - started, 3),
            "output": proc.stdout[-12000:],
        }
    except subprocess.TimeoutExpired as exc:
        output = ""
        if exc.stdout:
            output += exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode(errors="replace")
        if exc.stderr:
            output += exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace")
        return {"ok": False, "code": 124, "duration": round(time.time() - started, 3), "output": output[-12000:] + "\nTIMEOUT"}
    except Exception as exc:
        return {"ok": False, "code": 1, "duration": round(time.time() - started, 3), "output": str(exc)}


def start_process(name, args, cwd=None):
    ensure_dirs()
    path = log_path(name)
    fh = open(path, "ab", buffering=0)
    fh.write(f"# {time.strftime('%Y-%m-%d %H:%M:%S')} start: {' '.join(args)}\n".encode())
    proc = subprocess.Popen(
        args,
        cwd=str(cwd or HOME),
        stdout=fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"ok": True, "pid": proc.pid, "log": str(path)}


def docker_ps():
    res = run_cmd(["docker", "ps", "--format", "{{.Names}}|{{.Image}}|{{.Status}}"], timeout=5)
    rows = []
    if res["ok"]:
        for line in res["output"].splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3:
                rows.append({"name": parts[0], "image": parts[1], "status": parts[2]})
    return rows


def docker_all_names():
    res = run_cmd(["docker", "ps", "-a", "--format", "{{.Names}}"], timeout=5)
    if not res["ok"]:
        return set()
    return {line.strip() for line in res["output"].splitlines() if line.strip()}


def container_running(names):
    current = {row["name"] for row in docker_ps()}
    return [name for name in names if name in current]


def docker_rm(names):
    existing = [name for name in names if name in docker_all_names()]
    missing = [name for name in names if name not in existing]
    if not existing:
        return {"ok": True, "code": 0, "output": "没有正在运行的目标容器", "stopped": [], "missing": missing}
    res = run_cmd(["docker", "rm", "-f", *existing], timeout=10, cwd=DEPLOY_DIR)
    res["stopped"] = existing
    res["missing"] = missing
    if res["ok"]:
        res["output"] = "已停止: " + ", ".join(existing)
    return res


def run_livox_sleep():
    sleep_script = DEPLOY_DIR / "livox_sleep.sh"
    if sleep_script.exists():
        return run_cmd([str(sleep_script)], timeout=25, cwd=DEPLOY_DIR)
    return {"ok": False, "output": "livox_sleep.sh not installed"}


def action_stop_scan_runtime():
    names = CONTAINERS["lidar"] + CONTAINERS["camera"] + CONTAINERS["lio"] + CONTAINERS["bag"] + CONTAINERS["gs_bag"]
    stopped = docker_rm(names)
    sleep_res = run_livox_sleep()
    output = "\n".join(part for part in [stopped.get("output", ""), sleep_res.get("output", "")] if part)
    return {
        "ok": bool(stopped.get("ok") and sleep_res.get("ok")),
        "stop_processes": stopped,
        "sleep_lidar": sleep_res,
        "output": output,
    }


def docker_sigint_wait(name, timeout=75):
    if name not in docker_all_names():
        return {"ok": True, "stopped": [], "output": f"{name} not running"}
    # Send SIGINT to roslaunch inside the container.  With "docker compose run
    # -T" PID 1 is bash which does not forward signals to children.  The
    # compose file uses "pid: host" so we must NOT use "kill -INT -1" (that
    # would signal every process on the host).  Instead target roslaunch which
    # will gracefully shut down all ROS nodes (allowing savePCD to complete).
    signal_res = run_cmd(
        ["docker", "exec", name, "bash", "-c",
         "pkill -INT -f roslaunch 2>/dev/null; "
         "pkill -INT -f fastlivo_mapping 2>/dev/null; "
         "pkill -INT -f 'rosbag record' 2>/dev/null; "
         "exit 0"],
        timeout=8,
        cwd=DEPLOY_DIR,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if name not in {row["name"] for row in docker_ps()}:
            return {
                "ok": True,
                "stopped": [name],
                "output": f"SIGINT sent to {name}; container exited",
                "signal": signal_res,
            }
        time.sleep(1)
    force = docker_rm([name])
    return {
        "ok": False,
        "stopped": force.get("stopped", []),
        "output": f"{name} did not exit after SIGINT timeout; forced stop",
        "signal": signal_res,
        "force": force,
    }


def read_active_fastlivo_scan():
    try:
        return json.loads(FASTLIVO_ACTIVE_SCAN.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_active_fastlivo_scan(info):
    ensure_dirs()
    FASTLIVO_ACTIVE_SCAN.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_active_fastlivo_scan():
    try:
        FASTLIVO_ACTIVE_SCAN.unlink()
    except FileNotFoundError:
        pass


def safe_remove_path(path, root):
    path = pathlib.Path(path).resolve()
    root = pathlib.Path(root).resolve()
    if path == root or root not in path.parents:
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def clear_fastlivo_log_outputs():
    for path in (
        FASTLIVO_PCD_DIR / "all_raw_points.pcd",
        FASTLIVO_PCD_DIR / "all_downsampled_points.pcd",
        FASTLIVO_PCD_DIR / "lidar_poses.txt",
        FASTLIVO_LOG_DIR / "mat_out.txt",
        FASTLIVO_LOG_DIR / "mat_pre.txt",
        FASTLIVO_LOG_DIR / "result" / "JR_Mid360.txt",
    ):
        safe_remove_path(path, FASTLIVO_LOG_DIR)
    for path in (
        FASTLIVO_LOG_DIR / "image",
        FASTLIVO_LOG_DIR / "Colmap" / "sparse" / "0",
    ):
        safe_remove_path(path, FASTLIVO_LOG_DIR)
    (FASTLIVO_LOG_DIR / "image").mkdir(parents=True, exist_ok=True)
    (FASTLIVO_LOG_DIR / "pcd").mkdir(parents=True, exist_ok=True)
    (FASTLIVO_LOG_DIR / "result").mkdir(parents=True, exist_ok=True)


def exporter_path():
    deployed = TOOLS_DIR / "export_gs_livo_dataset.py"
    if deployed.exists():
        return deployed
    return ROOT / "tools" / "export_gs_livo_dataset.py"


def export_gs_livo_dataset(scan_dir, raw_bag=None):
    script = exporter_path()
    if not script.exists():
        return {"ok": False, "output": f"exporter missing: {script}"}
    args = [
        "python3",
        str(script),
        "--fastlivo-log",
        str(FASTLIVO_LOG_DIR),
        "--scan-dir",
        str(scan_dir),
        "--camera-yaml",
        str(FASTLIVO_CONFIG_DIR / "camera_pinhole.yaml"),
        "--mid360-yaml",
        str(FASTLIVO_CONFIG_DIR / "mid360.yaml"),
        "--scan-id",
        pathlib.Path(scan_dir).name,
    ]
    if raw_bag:
        args.extend(["--raw-bag", str(raw_bag)])
    res = run_cmd(args, timeout=180, cwd=DEPLOY_DIR)
    try:
        res["metadata"] = json.loads(res.get("output", "{}"))
    except Exception:
        pass
    return res


def copy_fastlivo_outputs(scan_dir, extra_logs=None, raw_bag=None):
    ensure_dirs()
    target = pathlib.Path(scan_dir)
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    missing = []
    for name in ("all_raw_points.pcd", "all_downsampled_points.pcd", "lidar_poses.txt"):
        src = FASTLIVO_PCD_DIR / name
        if src.exists():
            dst = target / name
            shutil.copy2(src, dst)
            copied.append(str(dst))
        else:
            missing.append(str(src))
    log_dir = target / "logs"
    for log in extra_logs or []:
        log_path_obj = pathlib.Path(log)
        if log_path_obj.exists():
            log_dir.mkdir(parents=True, exist_ok=True)
            dst = log_dir / log_path_obj.name
            shutil.copy2(log_path_obj, dst)
            copied.append(str(dst))
    raw_bag_path = pathlib.Path(raw_bag) if raw_bag else None
    copied_raw_bag = None
    if raw_bag_path and raw_bag_path.exists():
        dst = target / raw_bag_path.name
        if raw_bag_path.resolve() != dst.resolve():
            shutil.copy2(raw_bag_path, dst)
            copied.append(str(dst))
        copied_raw_bag = str(dst)
    metadata = {
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "scan_dir": str(target),
        "source_pcd_dir": str(FASTLIVO_PCD_DIR),
        "raw_bag": copied_raw_bag,
        "copied": copied,
        "missing": missing,
    }
    (target / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    gs_export = export_gs_livo_dataset(target, raw_bag=copied_raw_bag)
    metadata["gs_livo_export"] = {
        "ok": bool(gs_export.get("ok")),
        "metadata": gs_export.get("metadata"),
        "output": gs_export.get("output", "")[-4000:],
    }
    (target / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def list_fastlivo_maps():
    ensure_dirs()
    maps = []
    for scan_dir in sorted((p for p in FASTLIVO_MAP_ROOT.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True):
        files = []
        name_set = set()
        total_size = 0
        for name in ("all_raw_points.pcd", "all_downsampled_points.pcd", "metadata.json"):
            path = scan_dir / name
            if path.exists() and path.is_file():
                st = path.stat()
                total_size += st.st_size
                name_set.add(name)
                files.append({
                    "name": name,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "url": f"/api/fastlivo/maps/{urllib.parse.quote(scan_dir.name)}/{urllib.parse.quote(name)}",
                })
        meta = read_json_file(scan_dir / "metadata.json") if "metadata.json" in name_set else {}
        maps.append({
            "id": scan_dir.name,
            "path": str(scan_dir),
            "mtime": scan_dir.stat().st_mtime,
            "files": files,
            "has_raw": "all_raw_points.pcd" in name_set,
            "has_downsampled": "all_downsampled_points.pcd" in name_set,
            "total_size": total_size,
            "saved_at": meta.get("saved_at"),
            "copied_count": len(meta.get("copied") or []) if meta else None,
            "missing_count": len(meta.get("missing") or []) if meta else None,
        })
    return {"ok": True, "root": str(FASTLIVO_MAP_ROOT), "maps": maps}


def read_json_file(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_json_file(path, data):
    pathlib.Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_gs_datasets():
    ensure_dirs()
    datasets = []
    for ds_dir in sorted((p for p in GS_DATASET_ROOT.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True):
        metadata = read_json_file(ds_dir / "metadata.json")
        datasets.append({
            "id": ds_dir.name,
            "path": str(ds_dir),
            "mtime": ds_dir.stat().st_mtime,
            "ok": metadata.get("ok"),
            "image_count": metadata.get("image_count"),
            "pose_count": metadata.get("pose_count"),
            "matched_count": metadata.get("matched_count"),
            "sync": metadata.get("sync", {}),
            "errors": metadata.get("errors", []),
        })
    return {"ok": True, "root": str(GS_DATASET_ROOT), "datasets": datasets}


def parse_sync_target(target):
    if ":" not in target:
        raise ValueError(f"bad sync target: {target}")
    host, path = target.split(":", 1)
    if not host or not path:
        raise ValueError(f"bad sync target: {target}")
    return host, path.rstrip("/")


def action_gs_sync_latest():
    datasets = list_gs_datasets().get("datasets", [])
    if not datasets:
        return {"ok": False, "output": "no GS-LIVO dataset found"}
    ds = datasets[0]
    ds_dir = pathlib.Path(ds["path"])
    metadata_path = ds_dir / "metadata.json"
    metadata = read_json_file(metadata_path)
    try:
        host, remote_root = parse_sync_target(GS_SYNC_TARGET)
    except Exception as exc:
        return {"ok": False, "output": str(exc)}
    remote_dir = f"{remote_root}/{ds_dir.name}"
    mkdir_res = run_cmd(["ssh", "-o", "BatchMode=yes", host, "mkdir", "-p", remote_dir], timeout=20, cwd=HOME)
    if not mkdir_res.get("ok"):
        metadata.setdefault("sync", {})
        metadata["sync"].update({
            "target": f"{host}:{remote_dir}",
            "status": "failed",
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "error": mkdir_res.get("output", ""),
        })
        write_json_file(metadata_path, metadata)
        return {"ok": False, "dataset": ds_dir.name, "output": mkdir_res.get("output", ""), "step": "mkdir_remote"}
    rsync_res = run_cmd([
        "rsync",
        "-av",
        "--info=progress2",
        "-e",
        "ssh -o BatchMode=yes",
        str(ds_dir) + "/",
        f"{host}:{remote_dir}/",
    ], timeout=1800, cwd=HOME)
    metadata.setdefault("sync", {})
    metadata["sync"].update({
        "target": f"{host}:{remote_dir}",
        "status": "synced" if rsync_res.get("ok") else "failed",
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "output": rsync_res.get("output", "")[-4000:],
    })
    write_json_file(metadata_path, metadata)
    return {"ok": bool(rsync_res.get("ok")), "dataset": ds_dir.name, "target": f"{host}:{remote_dir}", "output": rsync_res.get("output", "")}


def fastlivo_map_file_response(writer, clean_path):
    prefix = "/api/fastlivo/maps/"
    rest = clean_path[len(prefix):]
    parts = pathlib.PurePosixPath(urllib.parse.unquote(rest)).parts
    if len(parts) != 2 or ".." in parts:
        text_response(writer, "bad map path", "400 Bad Request")
        return
    scan_id, filename = parts
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", scan_id):
        text_response(writer, "bad scan id", "400 Bad Request")
        return
    allowed = {"all_raw_points.pcd", "all_downsampled_points.pcd", "metadata.json"}
    if filename not in allowed:
        text_response(writer, "bad map file", "400 Bad Request")
        return
    path = (FASTLIVO_MAP_ROOT / scan_id / filename).resolve()
    root = FASTLIVO_MAP_ROOT.resolve()
    if root not in path.parents or not path.exists() or not path.is_file():
        text_response(writer, "not found", "404 Not Found")
        return
    body = path.read_bytes()
    ctype = "application/json; charset=utf-8" if path.suffix == ".json" else "application/octet-stream"
    headers = [
        "HTTP/1.1 200 OK",
        f"Content-Type: {ctype}",
        f"Content-Length: {len(body)}",
        "Cache-Control: no-store",
        "Connection: close",
        "",
        "",
    ]
    writer.write("\r\n".join(headers).encode("utf-8") + body)


def ros_env_cmd(inner):
    return [
        "docker",
        "compose",
        "run",
        "-T",
        "--rm",
        "fast-livo2",
        "bash",
        "-lc",
        "source /opt/ros/noetic/setup.bash; "
        "source /home/jr/fast_livo2_ws/devel/setup.bash; "
        + inner,
    ]


def named_ros_env_cmd(container_name, inner):
    return [
        "docker",
        "compose",
        "run",
        "-T",
        "--rm",
        "--name",
        container_name,
        "fast-livo2",
        "bash",
        "-lc",
        "source /opt/ros/noetic/setup.bash; "
        "source /home/jr/fast_livo2_ws/devel/setup.bash; "
        + inner,
    ]


def docker_exec_ros_cmd(container_name, inner):
    return [
        "docker",
        "exec",
        container_name,
        "bash",
        "-lc",
        "source /opt/ros/noetic/setup.bash; "
        "source /home/jr/fast_livo2_ws/devel/setup.bash; "
        + inner,
    ]


_DIR_SIZE_CACHE = {}
_DIR_SIZE_TTL_SEC = 30


def dir_size_bytes(path, cache_key=None):
    """Directory size with short TTL cache (status polls every few seconds)."""
    key = cache_key or str(path)
    now = time.time()
    cached = _DIR_SIZE_CACHE.get(key)
    if cached and now - cached[0] < _DIR_SIZE_TTL_SEC:
        return cached[1]
    root = pathlib.Path(path)
    total = 0
    if root.exists():
        if root.is_file():
            total = root.stat().st_size
        else:
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    try:
                        total += (pathlib.Path(dirpath) / name).stat().st_size
                    except OSError:
                        pass
    _DIR_SIZE_CACHE[key] = (now, total)
    return total


def disk_usage_info(path=None):
    target = pathlib.Path(path or HOME)
    try:
        usage = shutil.disk_usage(str(target))
        return {
            "path": str(target),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_percent": round(usage.used / usage.total * 100, 1) if usage.total else None,
        }
    except Exception:
        return {
            "path": str(target),
            "total_bytes": None,
            "used_bytes": None,
            "free_bytes": None,
            "used_percent": None,
        }


def storage_status():
    maps_bytes = dir_size_bytes(FASTLIVO_MAP_ROOT, "fast_livo2_maps")
    bags_bytes = dir_size_bytes(BAGS_DIR, "bags")
    return {
        "disk": disk_usage_info(HOME),
        "maps": {
            "path": str(FASTLIVO_MAP_ROOT),
            "bytes": maps_bytes,
            "count": sum(1 for p in FASTLIVO_MAP_ROOT.iterdir() if p.is_dir()) if FASTLIVO_MAP_ROOT.exists() else 0,
        },
        "bags": {
            "path": str(BAGS_DIR),
            "bytes": bags_bytes,
            "count": sum(1 for p in BAGS_DIR.glob("*.bag")) if BAGS_DIR.exists() else 0,
        },
    }


def api_status():
    uptime = ""
    loadavg = ""
    mem = {}
    try:
        uptime = pathlib.Path("/proc/uptime").read_text().split()[0]
        loadavg = pathlib.Path("/proc/loadavg").read_text().strip()
        info = pathlib.Path("/proc/meminfo").read_text().splitlines()
        vals = {}
        for line in info:
            key, value = line.split(":", 1)
            vals[key] = int(value.strip().split()[0])
        total = vals.get("MemTotal", 0)
        avail = vals.get("MemAvailable", 0)
        mem = {
            "total_mb": round(total / 1024),
            "available_mb": round(avail / 1024),
            "used_percent": round((1 - avail / total) * 100, 1) if total else None,
        }
    except Exception:
        pass

    net = run_cmd(["ip", "-br", "addr", "show", "enp1s0"], timeout=3)
    ping = run_cmd(["ping", "-c", "1", "-W", "1", "192.168.1.151"], timeout=3)
    containers = docker_ps()
    current_names = {row["name"] for row in containers}
    running = {
        key: [name for name in names if name in current_names]
        for key, names in CONTAINERS.items()
    }
    ros_container = next(
        (
            name
            for name in CONTAINERS["camera"] + CONTAINERS["fusion"] + CONTAINERS["lio"] + CONTAINERS["lidar"] + CONTAINERS["bag"]
            if name in current_names
        ),
        None,
    )
    if ros_container:
        topics = run_cmd(
            docker_exec_ros_cmd(
                ros_container,
                "timeout 3s rostopic list 2>/dev/null | sort | egrep 'livox|cloud_registered|aft_mapped|path|camera|rgb' || true",
            ),
            timeout=5,
        )
        topic_lines = [line.strip() for line in topics["output"].splitlines() if line.strip().startswith("/")] if topics["ok"] else []
    else:
        topic_lines = []
    return {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "host": run_cmd(["hostname"], timeout=2)["output"].strip(),
        "uptime_seconds": float(uptime) if uptime else None,
        "loadavg": loadavg,
        "memory": mem,
        "storage": storage_status(),
        "network": {
            "enp1s0": net["output"].strip(),
            "mid360_ping_ok": ping["ok"],
            "mid360_ping": ping["output"].strip().splitlines()[-2:] if ping["output"] else [],
        },
        "containers": containers,
        "running": running,
        "topics": topic_lines,
    }


def action_lidar_start():
    running = container_running(CONTAINERS["lidar"])
    if running:
        return {"ok": True, "message": "Mid360 driver already running", "running": running}
    cmd = named_ros_env_cmd(
        "mid360_driver",
        "roslaunch livox_ros_driver2 msg_MID360.launch xfer_format:=1 rviz_enable:=false",
    )
    return start_process("lidar", cmd, cwd=DEPLOY_DIR)


def action_lidar_stop():
    stopped = docker_rm(CONTAINERS["lidar"])
    sleep_res = run_livox_sleep()
    output = "\n".join(part for part in [stopped.get("output", ""), sleep_res.get("output", "")] if part)
    return {
        "ok": bool(stopped.get("ok") and sleep_res.get("ok")),
        "stop_driver": stopped,
        "sleep_lidar": sleep_res,
        "output": output,
    }


def action_lidar_check():
    script = DEPLOY_DIR / "check_mid360.sh"
    res = run_cmd([str(script)], timeout=20, cwd=DEPLOY_DIR)
    path = log_path("lidar-check")
    path.write_text(res["output"], encoding="utf-8", errors="replace")
    res["log"] = str(path)
    return res


def action_camera_start():
    running = container_running(CONTAINERS["camera"])
    if running:
        return {"ok": True, "message": "Hikrobot camera already running", "running": running}
    cmd = named_ros_env_cmd(
        "hikrobot_camera",
        "roslaunch jr_fastlivo_validation hikrobot_camera_continuous.launch",
    )
    return start_process("camera", cmd, cwd=DEPLOY_DIR)


def action_camera_stop():
    return docker_rm(CONTAINERS["camera"])


def action_fastlivo_start():
    running = container_running(CONTAINERS["fusion"])
    if running:
        active = read_active_fastlivo_scan()
        return {"ok": True, "message": "FAST-LIVO2 mapping already running", "running": running, "scan": active}
    ensure_dirs()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    scan_dir = FASTLIVO_MAP_ROOT / stamp
    scan_dir.mkdir(parents=True, exist_ok=True)
    clear_fastlivo_log_outputs()
    cmd = named_ros_env_cmd(
        "fast_livo2_mapping",
        "roslaunch jr_fastlivo_validation fast_livo2_saved_mapping.launch "
        "rviz:=false pcd_save_en:=true pcd_save_type:=0 pcd_save_interval:=-1 pcd_filter_size:=0.15 "
        "img_save_en:=true img_save_interval:=1 pose_output_en:=true colmap_output_en:=true",
    )
    res = start_process("fastlivo-saved", cmd, cwd=DEPLOY_DIR)
    raw_bag = scan_dir / f"{stamp}-gs-raw.bag"
    bag_cmd = named_ros_env_cmd(
        "fast_livo2_gs_raw_bag_record",
        "rosbag record --lz4 "
        f"-O {raw_bag} "
        "/left_camera/image /livox/lidar /livox/imu",
    )
    bag_res = start_process("gs-raw-bag", bag_cmd, cwd=DEPLOY_DIR)
    active = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "scan_dir": str(scan_dir),
        "log": res.get("log"),
        "raw_bag": str(raw_bag),
        "raw_bag_log": bag_res.get("log"),
        "launch": "jr_fastlivo_validation fast_livo2_saved_mapping.launch",
        "gs_dataset_dir": str(GS_DATASET_ROOT / stamp),
    }
    write_active_fastlivo_scan(active)
    res["scan_dir"] = str(scan_dir)
    res["raw_bag"] = str(raw_bag)
    res["raw_bag_record"] = bag_res
    return res


def action_fastlivo_stop():
    active = read_active_fastlivo_scan()
    if not container_running(CONTAINERS["fusion"]) and not active:
        runtime_stop = action_stop_scan_runtime()
        return {
            "ok": bool(runtime_stop.get("ok")),
            "message": "FAST-LIVO2 mapping is not running",
            "stop_runtime": runtime_stop,
            "output": "FAST-LIVO2 mapping is not running; runtime stop commands were sent.\n" + runtime_stop.get("output", ""),
        }
    scan_dir = active.get("scan_dir") or str(FASTLIVO_MAP_ROOT / time.strftime("%Y%m%d-%H%M%S"))
    stop_res = docker_sigint_wait("fast_livo2_mapping", timeout=90)
    raw_bag_stop = docker_sigint_wait("fast_livo2_gs_raw_bag_record", timeout=35)
    time.sleep(2)
    extra_logs = [p for p in (active.get("log"), active.get("raw_bag_log")) if p]
    saved = copy_fastlivo_outputs(scan_dir, extra_logs=extra_logs, raw_bag=active.get("raw_bag"))
    runtime_stop = action_stop_scan_runtime()
    clear_active_fastlivo_scan()
    ok = bool(stop_res.get("ok") and runtime_stop.get("ok") and (saved.get("copied") or not saved.get("missing")))
    output = [
        stop_res.get("output", ""),
        f"scan_dir: {scan_dir}",
        "copied:",
        *saved.get("copied", []),
        "raw_bag_stop:",
        raw_bag_stop.get("output", ""),
        "gs_livo_export:",
        json.dumps(saved.get("gs_livo_export", {}), ensure_ascii=False)[:2000],
        "runtime stop:",
        runtime_stop.get("output", ""),
    ]
    if saved.get("missing"):
        output.extend(["missing:", *saved.get("missing", [])])
    return {
        "ok": ok,
        "scan_dir": scan_dir,
        "stop_mapping": stop_res,
        "stop_raw_bag": raw_bag_stop,
        "save": saved,
        "stop_runtime": runtime_stop,
        "output": "\n".join(str(x) for x in output if x),
    }


def action_fastlivo_start_all():
    lio_stop = action_lio_stop()
    lidar = action_lidar_start()
    time.sleep(1)
    camera = action_camera_start()
    time.sleep(1)
    mapping = action_fastlivo_start()
    return {
        "ok": bool(lidar.get("ok") and camera.get("ok") and mapping.get("ok")),
        "lio_stop": lio_stop,
        "lidar": lidar,
        "camera": camera,
        "mapping": mapping,
        "scan_dir": mapping.get("scan_dir"),
    }


def action_lio_start():
    running = container_running(CONTAINERS["lio"])
    if running:
        return {"ok": True, "message": "JR扫描仪雷达建图已在运行", "running": running}
    cmd = named_ros_env_cmd(
        "jr_lidar_mapping",
        "roslaunch fast_lio mapping_mid360.launch rviz:=false",
    )
    return start_process("lidar-mapping", cmd, cwd=DEPLOY_DIR)


def action_lio_stop():
    return docker_rm(CONTAINERS["lio"])


def action_lio_start_all():
    lidar = action_lidar_start()
    time.sleep(1)
    mapping = action_lio_start()
    return {"ok": bool(lidar.get("ok") and mapping.get("ok")), "lidar": lidar, "mapping": mapping}


def action_stop_all():
    fastlivo_save = None
    if container_running(CONTAINERS["fusion"]):
        fastlivo_save = action_fastlivo_stop()
    runtime_stop = action_stop_scan_runtime()
    output_parts = []
    if fastlivo_save:
        output_parts.append(fastlivo_save.get("output", ""))
    output_parts.append(runtime_stop.get("output", ""))
    return {
        "ok": bool((fastlivo_save is None or fastlivo_save.get("ok")) and runtime_stop.get("ok")),
        "save_fastlivo": fastlivo_save,
        "stop_runtime": runtime_stop,
        "output": "\n".join(part for part in output_parts if part).strip(),
    }


def action_bag_start():
    running = container_running(CONTAINERS["bag"])
    if running:
        return {"ok": True, "message": "bag record already running", "running": running}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    inner = f"rosbag record -O /home/jr/fast_livo2_data/bags/mid360-{stamp}.bag /livox/lidar /livox/imu"
    cmd = named_ros_env_cmd("fast_livo2_bag_record", inner)
    res = start_process("bag-record", cmd, cwd=DEPLOY_DIR)
    res["bag"] = str(BAGS_DIR / f"mid360-{stamp}.bag")
    return res


def action_bag_stop():
    return docker_rm(CONTAINERS["bag"])


def action_perf_snapshot():
    script = DEPLOY_DIR / "perf_watch.sh"
    return run_cmd([str(script)], timeout=15, cwd=DEPLOY_DIR)


def recent_logs(target):
    ensure_dirs()
    allowed = {
        "lidar": "lidar*.log",
        "fastlivo": "fastlivo*.log",
        "lio": "lidar-mapping*.log",
        "bag": "bag*.log",
        "check": "lidar-check*.log",
        "perf": "perf*.log",
        "service": "console*.log",
    }
    pattern = allowed.get(target, "*.log")
    files = sorted(LOG_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    items = []
    for path in files[:12]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""
        items.append({"name": path.name, "path": str(path), "mtime": path.stat().st_mtime, "tail": text[-8000:]})
    return {"ok": True, "target": target, "logs": items}


def json_response(writer, data, status="200 OK"):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    headers = [
        f"HTTP/1.1 {status}",
        "Content-Type: application/json; charset=utf-8",
        f"Content-Length: {len(body)}",
        "Cache-Control: no-store",
        "Connection: close",
        "",
        "",
    ]
    writer.write("\r\n".join(headers).encode("utf-8") + body)


def text_response(writer, text, status="200 OK", content_type="text/plain; charset=utf-8"):
    body = text.encode("utf-8")
    headers = [
        f"HTTP/1.1 {status}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body)}",
        "Cache-Control: no-store",
        "Connection: close",
        "",
        "",
    ]
    writer.write("\r\n".join(headers).encode("utf-8") + body)


def static_response(writer, rel):
    if rel in ("", "/"):
        rel = "/index.html"
    rel_path = pathlib.PurePosixPath(urllib.parse.unquote(rel.lstrip("/")))
    if ".." in rel_path.parts:
        text_response(writer, "bad path", "400 Bad Request")
        return
    path = STATIC_DIR / pathlib.Path(*rel_path.parts)
    if not path.exists() or not path.is_file():
        text_response(writer, "not found", "404 Not Found")
        return
    body = path.read_bytes()
    ctype = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
    headers = [
        "HTTP/1.1 200 OK",
        f"Content-Type: {ctype}",
        f"Content-Length: {len(body)}",
        "Cache-Control: no-cache",
        "Connection: close",
        "",
        "",
    ]
    writer.write("\r\n".join(headers).encode("utf-8") + body)


def parse_request_head(data):
    head = data.decode("iso-8859-1", errors="replace")
    lines = head.split("\r\n")
    method, path, _version = lines[0].split(" ", 2)
    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return method, path, headers


def websocket_accept(key):
    value = key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    return base64.b64encode(hashlib.sha1(value.encode("ascii")).digest()).decode("ascii")


async def ws_send(writer, obj):
    if writer.is_closing():
        return
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    header = bytearray([0x81])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.extend([126, (length >> 8) & 255, length & 255])
    else:
        header.extend([127])
        header.extend(length.to_bytes(8, "big"))
    writer.write(bytes(header) + payload)
    await writer.drain()


async def stream_points(writer, mode, quality):
    ensure_dirs()
    safe_mode = mode if mode in ("lidar", "mapping") else "lidar"
    safe_quality = quality if quality in ("mini", "pc") else "mini"
    if safe_mode == "lidar":
        max_points = "12000" if safe_quality == "mini" else "28000"
        hz = "4"
        voxel_size = "0.00"
    else:
        max_points = "18000" if safe_quality == "mini" else "52000"
        hz = "3" if safe_quality == "mini" else "4"
        voxel_size = "0.03" if safe_quality == "mini" else "0.015"
    inner = (
        f"python3 /home/jr/fast_livo2_data/tools/ros_point_stream.py "
        f"--mode {safe_mode} --max-points {max_points} --hz {hz} --voxel-size {voxel_size}"
    )
    current = {row["name"] for row in docker_ps()}
    candidates = CONTAINERS["lidar"] if safe_mode == "lidar" else CONTAINERS["fusion"] + CONTAINERS["lio"] + CONTAINERS["lidar"]
    container_name = next((name for name in candidates if name in current), None)
    if not container_name:
        await ws_send(writer, {
            "type": "status",
            "level": "warn",
            "message": "请先启动雷达驱动" if safe_mode == "lidar" else "请先启动雷达建图",
        })
        return
    cmd = docker_exec_ros_cmd(container_name, inner)
    log = log_path(f"stream-{safe_mode}")
    await ws_send(writer, {"type": "status", "level": "info", "message": f"stream starting in {container_name}: {safe_mode}"})
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(DEPLOY_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        limit=8 * 1024 * 1024,
    )
    try:
        assert proc.stdout is not None
        with open(log, "ab", buffering=0) as fh:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                if text.startswith("{"):
                    try:
                        obj = json.loads(text)
                        if obj.get("type") == "points":
                            logged = {k: v for k, v in obj.items() if k != "points"}
                            fh.write((json.dumps(logged, ensure_ascii=False) + "\n").encode("utf-8"))
                        else:
                            fh.write(line)
                        await ws_send(writer, obj)
                    except Exception:
                        fh.write((text[-1000:] + "\n").encode("utf-8"))
                        await ws_send(writer, {"type": "log", "message": text[-1000:]})
                else:
                    fh.write(line)
                    await ws_send(writer, {"type": "log", "message": text[-1000:]})
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        if proc.returncode is None:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                proc.kill()


async def stream_camera(writer, quality):
    ensure_dirs()
    safe_quality = quality if quality in ("mini", "pc") else "mini"
    width = "480" if safe_quality == "mini" else "720"
    hz = "4" if safe_quality == "mini" else "6"
    inner = (
        "python3 /home/jr/fast_livo2_data/tools/ros_image_stream.py "
        f"--topics /rgb_img,/left_camera/image --hz {hz} --width {width} --quality 72"
    )
    current = {row["name"] for row in docker_ps()}
    if not any(name in current for name in CONTAINERS["camera"]):
        await ws_send(writer, {"type": "status", "level": "info", "message": "正在启动 Hikrobot 相机..."})
        action_camera_start()
        await asyncio.sleep(3)
        current = {row["name"] for row in docker_ps()}
    candidates = CONTAINERS["camera"] + CONTAINERS["fusion"] + CONTAINERS["lidar"] + CONTAINERS["lio"]
    container_name = next((name for name in candidates if name in current), None)
    if not container_name:
        await ws_send(writer, {"type": "status", "level": "warn", "message": "相机容器未启动，请检查 Hikrobot 连接"})
        return
    cmd = docker_exec_ros_cmd(container_name, inner)
    log = log_path("stream-camera")
    await ws_send(writer, {"type": "status", "level": "info", "message": f"camera stream starting in {container_name}"})
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(DEPLOY_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        limit=8 * 1024 * 1024,
    )
    try:
        assert proc.stdout is not None
        with open(log, "ab", buffering=0) as fh:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                if text.startswith("{"):
                    try:
                        obj = json.loads(text)
                        if obj.get("type") == "image":
                            logged = {k: v for k, v in obj.items() if k != "data"}
                            fh.write((json.dumps(logged, ensure_ascii=False) + "\n").encode("utf-8"))
                        else:
                            fh.write(line)
                        await ws_send(writer, obj)
                    except Exception:
                        fh.write((text[-1000:] + "\n").encode("utf-8"))
                        await ws_send(writer, {"type": "log", "message": text[-1000:]})
                else:
                    fh.write(line)
                    await ws_send(writer, {"type": "log", "message": text[-1000:]})
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        if proc.returncode is None:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                proc.kill()


async def handle_websocket(reader, writer, path, headers, kind):
    key = headers.get("sec-websocket-key", "")
    if not key:
        text_response(writer, "missing websocket key", "400 Bad Request")
        return
    accept = websocket_accept(key)
    writer.write(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        ).encode("ascii")
    )
    await writer.drain()
    query = urllib.parse.urlparse(path).query
    params = urllib.parse.parse_qs(query)
    mode = params.get("mode", ["lidar"])[0]
    quality = params.get("quality", ["mini"])[0]
    if kind == "camera":
        await stream_camera(writer, quality)
    else:
        await stream_points(writer, mode, quality)


async def handle_http(reader, writer):
    try:
        data = await reader.readuntil(b"\r\n\r\n")
    except Exception:
        writer.close()
        await writer.wait_closed()
        return

    try:
        method, path, headers = parse_request_head(data)
    except Exception:
        text_response(writer, "bad request", "400 Bad Request")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        return

    parsed = urllib.parse.urlparse(path)
    clean_path = parsed.path

    if headers.get("upgrade", "").lower() == "websocket" and clean_path in ("/ws/points", "/ws/camera"):
        await handle_websocket(reader, writer, path, headers, "camera" if clean_path == "/ws/camera" else "points")
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError):
            pass
        return

    try:
        if clean_path == "/api/status" and method == "GET":
            json_response(writer, api_status())
        elif clean_path == "/api/fastlivo/maps" and method == "GET":
            json_response(writer, list_fastlivo_maps())
        elif clean_path.startswith("/api/fastlivo/maps/") and method == "GET":
            fastlivo_map_file_response(writer, clean_path)
        elif clean_path == "/api/gs/datasets" and method == "GET":
            json_response(writer, list_gs_datasets())
        elif clean_path == "/api/gs/sync_latest" and method == "POST":
            json_response(writer, action_gs_sync_latest())
        elif clean_path == "/api/logs" and method == "GET":
            params = urllib.parse.parse_qs(parsed.query)
            target = params.get("target", ["all"])[0]
            json_response(writer, recent_logs(target))
        elif method == "POST" and clean_path == "/api/lidar/start":
            json_response(writer, action_lidar_start())
        elif method == "POST" and clean_path == "/api/lidar/stop":
            json_response(writer, action_lidar_stop())
        elif method == "POST" and clean_path == "/api/lidar/check":
            json_response(writer, action_lidar_check())
        elif method == "POST" and clean_path == "/api/camera/start":
            json_response(writer, action_camera_start())
        elif method == "POST" and clean_path == "/api/camera/stop":
            json_response(writer, action_camera_stop())
        elif method == "POST" and clean_path == "/api/fastlivo/start":
            json_response(writer, action_fastlivo_start_all())
        elif method == "POST" and clean_path == "/api/fastlivo/stop":
            json_response(writer, action_fastlivo_stop())
        elif method == "POST" and clean_path == "/api/fastlivo/start_all":
            json_response(writer, action_fastlivo_start_all())
        elif method == "POST" and clean_path == "/api/lio/start":
            json_response(writer, action_lio_start())
        elif method == "POST" and clean_path == "/api/lio/stop":
            json_response(writer, action_lio_stop())
        elif method == "POST" and clean_path == "/api/lio/start_all":
            json_response(writer, action_lio_start_all())
        elif method == "POST" and clean_path == "/api/stop_all":
            json_response(writer, action_stop_all())
        elif method == "POST" and clean_path == "/api/bag/start":
            json_response(writer, action_bag_start())
        elif method == "POST" and clean_path == "/api/bag/stop":
            json_response(writer, action_bag_stop())
        elif method == "POST" and clean_path == "/api/perf/snapshot":
            json_response(writer, action_perf_snapshot())
        else:
            static_response(writer, clean_path)
    except Exception as exc:
        json_response(writer, {"ok": False, "error": str(exc)}, "500 Internal Server Error")

    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def main():
    ensure_dirs()
    server = await asyncio.start_server(handle_http, HOST, PORT)
    print(f"JR scanner console listening on http://{HOST}:{PORT}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
