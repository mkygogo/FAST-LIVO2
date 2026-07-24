#!/usr/bin/env python3
import asyncio
import base64
import hashlib
import json
import os
import pathlib
import re
import shlex
import signal
import shutil
import subprocess
import threading
import time
import urllib.parse
import uuid


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
FASTLIVO_ACTIVE_RECORDING = OUTPUT_DIR / "active_fast_livo2_recording.json"
FASTLIVO_OFFLINE_JOB = OUTPUT_DIR / "fast_livo2_offline_job.json"
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
    "offline_play": ["fast_livo2_offline_bag_play"],
}

WORKFLOW_LOCK = threading.RLock()
GIB = 1024 ** 3
RECORD_WARN_FREE = 30 * GIB
RECORD_MIN_START_FREE = 15 * GIB
RECORD_AUTO_STOP_FREE = 8 * GIB

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
    names = CONTAINERS["lidar"] + CONTAINERS["camera"] + CONTAINERS["lio"] + CONTAINERS["bag"] + CONTAINERS["gs_bag"] + CONTAINERS["offline_play"]
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
         "pkill -INT -f 'rosbag play' 2>/dev/null; "
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


def fastlivo_processing_status():
    """Read mapper progress and queue sizes without stopping the container."""
    if "fast_livo2_mapping" not in docker_all_names():
        return {"ok": False, "lag": None, "output": "fast_livo2_mapping not running"}
    res = run_cmd(
        [
            "docker", "exec", "fast_livo2_mapping", "bash", "-lc",
            "source /opt/ros/noetic/setup.bash; "
            "source /home/jr/fast_livo2_ws/devel/setup.bash; "
            "timeout 3 rostopic echo -n 1 /fast_livo2/processing_status",
        ],
        timeout=5,
        cwd=DEPLOY_DIR,
    )
    match = re.search(r"data:\s*\[([^\]]+)\]", res.get("output", ""), re.S)
    values = []
    if match:
        try:
            values = [float(value.strip()) for value in match.group(1).replace("\n", " ").split(",") if value.strip()]
        except ValueError:
            values = []
    if len(values) < 9:
        return {"ok": False, "lag": None, "output": res.get("output", "")[-1000:]}
    keys = (
        "last_received", "last_processed", "lag", "lidar_buffer", "image_buffer",
        "imu_buffer", "image_save_queue", "image_save_written", "image_save_dropped",
    )
    status = dict(zip(keys, values[:9]))
    for key in ("lidar_buffer", "image_buffer", "imu_buffer", "image_save_queue", "image_save_written", "image_save_dropped"):
        status[key] = int(status[key])
    status.update({"ok": True, "output": res.get("output", "")[-1000:]})
    return status


def fastlivo_processing_lag():
    status = fastlivo_processing_status()
    if status.get("ok"):
        return status
    if "fast_livo2_mapping" not in docker_all_names():
        return status
    res = run_cmd(
        docker_exec_ros_cmd("fast_livo2_mapping", "timeout 3 rostopic echo -n 1 /fast_livo2/processing_lag"),
        timeout=5,
        cwd=DEPLOY_DIR,
    )
    match = re.search(r"(?:^|\n)data:\s*([0-9]+(?:\.[0-9]+)?)", res.get("output", ""))
    lag = float(match.group(1)) if match else None
    return {"ok": lag is not None, "lag": lag, "output": res.get("output", "")[-500:]}


def wait_fastlivo_catch_up(max_wait=30, target_lag=0.5):
    """Give FAST-LIVO2 time to consume queued sensor frames before SIGINT."""
    started = time.time()
    samples = []
    consecutive_ready = 0
    while time.time() - started < max_wait:
        probe = fastlivo_processing_lag()
        lag = probe.get("lag")
        if lag is None:
            return {
                "ok": False,
                "available": False,
                "waited": round(time.time() - started, 1),
                "output": "processing lag topic unavailable; continuing graceful stop",
            }
        samples.append(round(lag, 3))
        if lag <= target_lag:
            consecutive_ready += 1
            if consecutive_ready >= 2:
                return {
                    "ok": True,
                    "available": True,
                    "waited": round(time.time() - started, 1),
                    "lag": lag,
                    "samples": samples[-10:],
                    "output": f"FAST-LIVO2 caught up; processing lag={lag:.3f}s",
                }
        else:
            consecutive_ready = 0
        time.sleep(1)
    lag = samples[-1] if samples else None
    return {
        "ok": False,
        "available": True,
        "waited": round(time.time() - started, 1),
        "lag": lag,
        "samples": samples[-10:],
        "output": f"FAST-LIVO2 still has {lag:.3f}s processing lag after {max_wait}s" if lag is not None else "processing lag unavailable",
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


def atomic_write_json(path, data):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_active_recording():
    return read_json_file(FASTLIVO_ACTIVE_RECORDING)


def write_active_recording(info):
    atomic_write_json(FASTLIVO_ACTIVE_RECORDING, info)


def clear_active_recording():
    try:
        FASTLIVO_ACTIVE_RECORDING.unlink()
    except FileNotFoundError:
        pass


def read_offline_job():
    return read_json_file(FASTLIVO_OFFLINE_JOB)


def write_offline_job(info):
    atomic_write_json(FASTLIVO_OFFLINE_JOB, info)


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
    if raw_bag:
        source_bag = pathlib.Path(raw_bag).resolve()
        exported_bag = GS_DATASET_ROOT / pathlib.Path(scan_dir).name / "raw" / source_bag.name
        try:
            if exported_bag.exists() or exported_bag.is_symlink():
                if exported_bag.exists() and os.path.samefile(source_bag, exported_bag):
                    pass
                else:
                    exported_bag.unlink()
            if not exported_bag.exists() and not exported_bag.is_symlink():
                try:
                    os.link(source_bag, exported_bag)
                except OSError:
                    os.symlink(source_bag, exported_bag)
        except OSError as exc:
            res["raw_bag_link_warning"] = str(exc)
    try:
        res["metadata"] = json.loads(res.get("output", "{}"))
    except Exception:
        pass
    return res


def archive_exported_dataset(scan_dir, copied=None):
    """Mirror training inputs into the saved map directory.

    The exporter keeps its complete working copy under gs_livo_datasets.  The
    scan archive also needs the operator-facing subset so a map directory is
    self-contained and can be pulled by the offline LOD-3DGS converter.
    """
    target = pathlib.Path(scan_dir)
    dataset = GS_DATASET_ROOT / target.name
    copied = copied if copied is not None else []
    errors = []
    copy_count = 0

    file_mappings = (
        (dataset / "raw" / "image_poses.txt", target / "image_poses.txt"),
        (dataset / "raw" / "camera_pinhole.yaml", target / "calib" / "camera_pinhole.yaml"),
        (dataset / "raw" / "mid360.yaml", target / "calib" / "mid360.yaml"),
    )
    tree_mappings = (
        (dataset / "colmap" / "images", target / "images"),
        (dataset / "colmap" / "sparse", target / "colmap" / "sparse"),
    )

    try:
        for src, dst in file_mappings:
            if not src.is_file():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(str(dst))
            copy_count += 1
        for src_root, dst_root in tree_mappings:
            if not src_root.is_dir():
                continue
            for src in sorted(path for path in src_root.rglob("*") if path.is_file()):
                dst = dst_root / src.relative_to(src_root)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied.append(str(dst))
                copy_count += 1
    except Exception as exc:
        errors.append(str(exc))

    required = (
        target / "image_poses.txt",
        target / "images",
        target / "calib" / "camera_pinhole.yaml",
        target / "colmap" / "sparse" / "0" / "cameras.txt",
        target / "colmap" / "sparse" / "0" / "images.txt",
        target / "colmap" / "sparse" / "0" / "points3D.txt",
    )
    missing = [str(path) for path in required if not path.exists()]
    return {
        "ok": not errors and not missing,
        "dataset_dir": str(dataset),
        "copied_count": copy_count,
        "missing": missing,
        "errors": errors,
    }


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
    metadata["dataset_archive"] = archive_exported_dataset(target, copied)
    metadata["copied"] = list(dict.fromkeys(copied))
    (target / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def filesystem_free_bytes(path=DATA_DIR):
    usage = shutil.disk_usage(path)
    return usage.free


def find_scan_bag(scan_dir):
    scan_dir = pathlib.Path(scan_dir)
    bags = sorted(
        (path for path in scan_dir.glob("*.bag") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return bags[0] if bags else None


def inspect_rosbag(path):
    path = pathlib.Path(path)
    if not path.is_file():
        return {"ok": False, "valid": False, "errors": [f"bag不存在: {path}"]}
    cmd = ros_env_cmd(f"rosbag info --yaml {shlex.quote(str(path))}")
    res = run_cmd(cmd, timeout=90, cwd=DEPLOY_DIR)
    output = res.get("output", "")
    info = {"path": str(path), "size": path.stat().st_size, "topics": {}, "errors": [], "warnings": []}
    for key in ("duration", "start", "end"):
        match = re.search(rf"(?m)^{key}:\s*([0-9]+(?:\.[0-9]+)?)", output)
        if match:
            info[key] = float(match.group(1))
    info["indexed"] = bool(re.search(r"(?m)^indexed:\s*True\s*$", output))
    for match in re.finditer(r"(?ms)^\s*- topic:\s*(\S+).*?^\s+messages:\s*(\d+)\s*$", output):
        info["topics"][match.group(1)] = int(match.group(2))
    duration = float(info.get("duration") or 0.0)
    required_rates = {"/left_camera/image": 8.0, "/livox/lidar": 5.0, "/livox/imu": 100.0}
    if not res.get("ok"):
        info["errors"].append("rosbag info读取失败")
    if not info.get("indexed"):
        info["errors"].append("bag索引未完成")
    if duration < 1.0:
        info["errors"].append("录制时长不足1秒")
    for topic, min_rate in required_rates.items():
        count = info["topics"].get(topic, 0)
        if count <= 0:
            info["errors"].append(f"缺少{topic}")
        elif duration >= 3.0 and count / duration < min_rate:
            info["errors"].append(f"{topic}平均频率过低: {count / duration:.1f}Hz")
    info["valid"] = not info["errors"]
    info["ok"] = bool(res.get("ok"))
    info["output"] = output[-2000:]
    return info


def scan_workflow_status(scan_dir, metadata, has_map, has_bag):
    workflow = metadata.get("workflow") or {}
    offline = workflow.get("offline") or {}
    recording = workflow.get("recording") or {}
    status = offline.get("status") or recording.get("status")
    if status in ("running", "failed", "cancelled"):
        return status
    if status in ("valid", "invalid") and not has_map:
        return "ready" if status == "valid" else "invalid"
    if has_map:
        return "completed"
    if has_bag:
        return "ready"
    return "invalid"


def list_fastlivo_scans():
    ensure_dirs()
    scans = []
    active_record = read_active_recording()
    offline_job = read_offline_job()
    for scan_dir in sorted((p for p in FASTLIVO_MAP_ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.stat().st_mtime, reverse=True):
        metadata = read_json_file(scan_dir / "metadata.json")
        bag = find_scan_bag(scan_dir)
        raw_pcd = scan_dir / "all_raw_points.pcd"
        down_pcd = scan_dir / "all_downsampled_points.pcd"
        has_map = raw_pcd.is_file() or down_pcd.is_file()
        status = scan_workflow_status(scan_dir, metadata, has_map, bag is not None)
        if active_record.get("scan_id") == scan_dir.name:
            status = "recording"
        if offline_job.get("scan_id") == scan_dir.name and offline_job.get("status") in ("starting", "running", "draining", "saving", "cancel_requested"):
            status = "running"
        files = []
        for path in (bag, raw_pcd if raw_pcd.is_file() else None, down_pcd if down_pcd.is_file() else None, scan_dir / "metadata.json"):
            if path and pathlib.Path(path).is_file():
                st = pathlib.Path(path).stat()
                files.append({"name": pathlib.Path(path).name, "size": st.st_size, "mtime": st.st_mtime})
        bag_meta = ((metadata.get("workflow") or {}).get("recording") or {}).get("bag_info") or {}
        scans.append({
            "id": scan_dir.name,
            "path": str(scan_dir),
            "mtime": scan_dir.stat().st_mtime,
            "status": status,
            "files": files,
            "bag": str(bag) if bag else None,
            "bag_size": bag.stat().st_size if bag else None,
            "bag_duration": bag_meta.get("duration"),
            "bag_valid": bag_meta.get("valid") if bag_meta else None,
            "has_raw": raw_pcd.is_file(),
            "has_downsampled": down_pcd.is_file(),
            "has_map": has_map,
            "can_offline_map": bool(bag and status not in ("recording", "running", "invalid")),
            "can_delete": status not in ("recording", "running"),
            "saved_at": metadata.get("saved_at"),
            "workflow": metadata.get("workflow") or {},
            "total_size": sum(item["size"] for item in files),
        })
    return {"ok": True, "root": str(FASTLIVO_MAP_ROOT), "scans": scans}


def action_fastlivo_scan_delete(scan_id):
    """Permanently delete one scan directory under fast_livo2_maps (and matching gs export)."""
    with WORKFLOW_LOCK:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", scan_id or ""):
            return {"ok": False, "message": "扫描ID不合法"}
        scan_dir = (FASTLIVO_MAP_ROOT / scan_id).resolve()
        root = FASTLIVO_MAP_ROOT.resolve()
        if scan_dir.parent != root or not scan_dir.is_dir():
            return {"ok": False, "message": "扫描记录不存在"}
        active_record = read_active_recording()
        if active_record.get("scan_id") == scan_id:
            return {"ok": False, "message": "该记录正在录制，不能删除"}
        offline_job = read_offline_job()
        if offline_job.get("scan_id") == scan_id and offline_job.get("status") in (
            "starting", "running", "draining", "saving", "cancel_requested",
        ):
            return {"ok": False, "message": "该记录正在离线建图，请先停止后再删除"}
        active_scan = read_active_fastlivo_scan()
        if active_scan.get("scan_id") == scan_id or pathlib.Path(str(active_scan.get("scan_dir") or "")).name == scan_id:
            return {"ok": False, "message": "该记录正在实时建图，不能删除"}
        size_before = 0
        try:
            for path in scan_dir.rglob("*"):
                if path.is_file():
                    size_before += path.stat().st_size
        except OSError:
            size_before = 0
        safe_remove_path(scan_dir, FASTLIVO_MAP_ROOT)
        gs_dir = (GS_DATASET_ROOT / scan_id).resolve()
        gs_removed = False
        if gs_dir.is_dir() and gs_dir.parent == GS_DATASET_ROOT.resolve():
            safe_remove_path(gs_dir, GS_DATASET_ROOT)
            gs_removed = not gs_dir.exists()
        if scan_dir.exists():
            return {"ok": False, "message": f"删除失败，目录仍存在: {scan_dir}"}
        return {
            "ok": True,
            "scan_id": scan_id,
            "freed_bytes": size_before,
            "gs_dataset_removed": gs_removed,
            "message": f"已删除扫描数据 {scan_id}",
        }


def update_scan_workflow(scan_dir, section, values):
    scan_dir = pathlib.Path(scan_dir)
    metadata_path = scan_dir / "metadata.json"
    metadata = read_json_file(metadata_path)
    metadata.setdefault("scan_id", scan_dir.name)
    metadata.setdefault("scan_dir", str(scan_dir))
    workflow = metadata.setdefault("workflow", {})
    current = workflow.setdefault(section, {})
    current.update(values)
    atomic_write_json(metadata_path, metadata)
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
    atomic_write_json(path, data)


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
        "--copy-links",
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


def named_ros_env_cmd(container_name, inner, remove=True):
    args = [
        "docker",
        "compose",
        "run",
        "-T",
    ]
    if remove:
        args.append("--rm")
    args.extend([
        "--name", container_name, "fast-livo2", "bash", "-lc",
        "source /opt/ros/noetic/setup.bash; "
        "source /home/jr/fast_livo2_ws/devel/setup.bash; "
        + inner,
    ])
    return args


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
    workflow = active_workflow()
    processing = {}
    if workflow == "realtime_mapping" and "fast_livo2_mapping" in current_names:
        processing = fastlivo_processing_status()
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
        "workflow": workflow,
        "recording": record_runtime_status(),
        "offline": read_offline_job(),
        "processing": processing,
        "recording_limits": {
            "warn_free_bytes": RECORD_WARN_FREE,
            "min_start_free_bytes": RECORD_MIN_START_FREE,
            "auto_stop_free_bytes": RECORD_AUTO_STOP_FREE,
        },
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


CAMERA_CONFIG_PATH = (
    HOME
    / "fast_livo2_ws"
    / "src"
    / "jr_fastlivo_validation"
    / "config"
    / "hikrobot_camera_continuous_calib.yaml"
)

# Fields the touch UI may edit. Driver applies these only at process start.
CAMERA_CONFIG_DEFAULTS = {
    "width": 2448,
    "height": 2048,
    "Offset_x": 0,
    "Offset_y": 0,
    "FrameRateEnable": True,
    "FrameRate": 10,
    "ExposureTime": 6000,
    "ExposureAutoString": "Off",
    "AutoExposureTimeLowerLimit": 100,
    "AutoExposureTimeUpperLimit": 10000,
    "AutoExposureAOIUsageIntensity": True,
    "AutoExposureAOIWidth": 1840,
    "AutoExposureAOIHeight": 1536,
    "AutoExposureAOIOffsetX": 304,
    "AutoExposureAOIOffsetY": 256,
    "GammaEnable": True,
    "Gamma": 0.7,
    "GainAuto": 2,
    "SaturationEnable": False,
    "Saturation": 128,
    "TriggerModeString": "Off",
}

CAMERA_PRESETS = {
    "indoor": {
        "label": "室内",
        "description": "普通室内：压住灯光高光并保留暗部",
        "ExposureTime": 6000,
        "ExposureAutoString": "Off",
        "GainAuto": 0,
        "GammaEnable": True,
        "Gamma": 0.7,
        "FrameRate": 10,
        "FrameRateEnable": True,
    },
    "outdoor": {
        "label": "室外",
        "description": "普通日光：缩短曝光，自动增益",
        "ExposureTime": 2000,
        "ExposureAutoString": "Off",
        "GainAuto": 2,
        "GammaEnable": True,
        "Gamma": 0.7,
        "FrameRate": 10,
        "FrameRateEnable": True,
    },
    "outdoor_bright": {
        "label": "强光",
        "description": "强光环境：极短曝光，固定增益",
        "ExposureTime": 800,
        "ExposureAutoString": "Off",
        "GainAuto": 0,
        "GammaEnable": True,
        "Gamma": 0.7,
        "FrameRate": 10,
        "FrameRateEnable": True,
    },
}


def _yaml_scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text if text else "0"
    if isinstance(value, int):
        return str(value)
    if value is None:
        return "null"
    text = str(value)
    yaml_keywords = {"true", "false", "yes", "no", "on", "off", "null", "~"}
    if text.lower() in yaml_keywords or re.search(r'[:#\[\]{},&*!|>\'"%@`]', text) or text.strip() != text:
        return json.dumps(text, ensure_ascii=False)
    return text


def parse_simple_yaml(path):
    """Minimal YAML subset used by hikrobot camera config files."""
    data = {}
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except Exception as exc:
        return {}, str(exc)
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        low = value.lower()
        if key.endswith("String"):
            data[key] = value[1:-1] if ((value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'"))) else value
        elif (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            data[key] = value[1:-1]
        elif low in ("true", "yes", "on"):
            data[key] = True
        elif low in ("false", "no", "off"):
            data[key] = False
        elif re.fullmatch(r"-?\d+", value):
            data[key] = int(value)
        elif re.fullmatch(r"-?\d+\.\d+", value):
            data[key] = float(value)
        else:
            data[key] = value
    return data, None


def write_camera_config_yaml(path, params):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Hikrobot continuous camera config (managed by JR console camera page).",
        "# Applied only when hikrobot_camera process starts — restart camera after changes.",
        f"# updated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    order = [
        "width",
        "height",
        "Offset_x",
        "Offset_y",
        "FrameRateEnable",
        "FrameRate",
        "ExposureTime",
        "ExposureAutoString",
        "AutoExposureTimeLowerLimit",
        "AutoExposureTimeUpperLimit",
        "AutoExposureAOIUsageIntensity",
        "AutoExposureAOIWidth",
        "AutoExposureAOIHeight",
        "AutoExposureAOIOffsetX",
        "AutoExposureAOIOffsetY",
        "GammaEnable",
        "Gamma",
        "GainAuto",
        "SaturationEnable",
        "Saturation",
        "TriggerModeString",
    ]
    for key in order:
        if key in params:
            lines.append(f"{key}: {_yaml_scalar(params[key])}")
    for key, value in params.items():
        if key not in order:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def normalize_camera_params(raw):
    """Whitelist and clamp camera parameters from UI."""
    base = dict(CAMERA_CONFIG_DEFAULTS)
    if not isinstance(raw, dict):
        return base, ["payload must be object"]
    errors = []

    def as_bool(key, default):
        if key not in raw:
            return default
        val = raw[key]
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return bool(val)
        if isinstance(val, str):
            low = val.strip().lower()
            if low in ("true", "1", "yes", "on"):
                return True
            if low in ("false", "0", "no", "off"):
                return False
        errors.append(f"{key}: invalid bool")
        return default

    def as_int(key, default, lo, hi):
        if key not in raw:
            return default
        try:
            val = int(float(raw[key]))
        except Exception:
            errors.append(f"{key}: invalid int")
            return default
        return max(lo, min(hi, val))

    def as_float(key, default, lo, hi):
        if key not in raw:
            return default
        try:
            val = float(raw[key])
        except Exception:
            errors.append(f"{key}: invalid float")
            return default
        return max(lo, min(hi, val))

    mode = str(raw.get("ExposureAutoString", base["ExposureAutoString"])).strip().lower()
    mode = {"off": "Off", "once": "Once", "continuous": "Continuous"}.get(mode)
    if mode is None:
        errors.append("ExposureAutoString: expected Off, Once or Continuous")
        mode = "Off"

    lower = as_int("AutoExposureTimeLowerLimit", base["AutoExposureTimeLowerLimit"], 15, 49999)
    upper = as_int("AutoExposureTimeUpperLimit", base["AutoExposureTimeUpperLimit"], 1000, 50000)
    if upper <= lower:
        errors.append("AutoExposureTimeUpperLimit: must be greater than lower limit")
        upper = min(50000, lower + 1)

    def aligned_int(key, default, lo, hi):
        return (as_int(key, default, lo, hi) // 4) * 4

    out = {
        "width": as_int("width", base["width"], 320, 4096),
        "height": as_int("height", base["height"], 240, 4096),
        "Offset_x": as_int("Offset_x", base["Offset_x"], 0, 4096),
        "Offset_y": as_int("Offset_y", base["Offset_y"], 0, 4096),
        "FrameRateEnable": as_bool("FrameRateEnable", base["FrameRateEnable"]),
        "FrameRate": as_int("FrameRate", base["FrameRate"], 1, 60),
        "ExposureTime": as_int("ExposureTime", base["ExposureTime"], 50, 100000),
        "ExposureAutoString": mode,
        "AutoExposureTimeLowerLimit": lower,
        "AutoExposureTimeUpperLimit": upper,
        "AutoExposureAOIUsageIntensity": as_bool(
            "AutoExposureAOIUsageIntensity", base["AutoExposureAOIUsageIntensity"]
        ),
        "AutoExposureAOIWidth": aligned_int("AutoExposureAOIWidth", base["AutoExposureAOIWidth"], 32, 4096),
        "AutoExposureAOIHeight": aligned_int("AutoExposureAOIHeight", base["AutoExposureAOIHeight"], 32, 4096),
        "AutoExposureAOIOffsetX": aligned_int("AutoExposureAOIOffsetX", base["AutoExposureAOIOffsetX"], 0, 4096),
        "AutoExposureAOIOffsetY": aligned_int("AutoExposureAOIOffsetY", base["AutoExposureAOIOffsetY"], 0, 4096),
        "GammaEnable": as_bool("GammaEnable", base["GammaEnable"]),
        "Gamma": as_float("Gamma", base["Gamma"], 0.1, 4.0),
        "GainAuto": as_int("GainAuto", base["GainAuto"], 0, 2),
        "SaturationEnable": as_bool("SaturationEnable", base["SaturationEnable"]),
        "Saturation": as_int("Saturation", base["Saturation"], 0, 255),
        "TriggerModeString": "Off",
    }
    if mode != "Off":
        out["GainAuto"] = 0
    return out, errors


def camera_config_status():
    path = CAMERA_CONFIG_PATH
    parsed, err = parse_simple_yaml(path) if path.exists() else ({}, "config missing")
    params = dict(CAMERA_CONFIG_DEFAULTS)
    params.update({k: parsed[k] for k in CAMERA_CONFIG_DEFAULTS if k in parsed})
    # keep unknown keys from file for completeness
    for k, v in parsed.items():
        if k not in params:
            params[k] = v
    running = container_running(CONTAINERS["camera"])
    return {
        "ok": True,
        "path": str(path),
        "exists": path.exists(),
        "read_error": err,
        "params": params,
        "running": running,
        "camera_running": bool(running),
        "presets": {
            key: {
                "label": val["label"],
                "description": val.get("description", ""),
                "params": {k: v for k, v in val.items() if k not in ("label", "description")},
            }
            for key, val in CAMERA_PRESETS.items()
        },
        "note": "相机通常由开始建图流程自动启动；启动/停止仅用于异常排查。参数应用后会重启 hikrobot_camera。",
        "limits": {
            "ExposureTime": {"min": 50, "max": 100000, "unit": "us"},
            "ExposureAutoString": {"values": ["Off", "Once", "Continuous"]},
            "AutoExposureTime": {"min": 15, "max": 50000, "unit": "us"},
            "FrameRate": {"min": 1, "max": 60},
            "Gamma": {"min": 0.1, "max": 4.0},
            "GainAuto": {"values": {"0": "Off", "1": "Once", "2": "Continuous"}},
            "Saturation": {"min": 0, "max": 255},
        },
    }


def action_camera_config_apply(body, restart=True):
    params, errors = normalize_camera_params(body if isinstance(body, dict) else {})
    if errors and body is not None:
        # still apply clamped values; surface parse issues
        pass
    write_camera_config_yaml(CAMERA_CONFIG_PATH, params)
    result = {
        "ok": True,
        "path": str(CAMERA_CONFIG_PATH),
        "params": params,
        "errors": errors,
        "restarted": False,
        "output": f"已写入 {CAMERA_CONFIG_PATH}",
    }
    if restart:
        stop_res = action_camera_stop()
        start_res = action_camera_start()
        result["restarted"] = True
        result["stop"] = stop_res
        result["start"] = start_res
        result["ok"] = bool(start_res.get("ok"))
        result["output"] = (
            f"已写入配置并重启相机。"
            f" stop={stop_res.get('ok')} start={start_res.get('ok')}"
        )
        if start_res.get("output"):
            result["output"] += "\n" + str(start_res.get("output"))[-800:]
    return result


def action_camera_preset(preset_id, restart=True):
    preset = CAMERA_PRESETS.get(preset_id)
    if not preset:
        return {"ok": False, "output": f"unknown preset: {preset_id}", "presets": list(CAMERA_PRESETS)}
    current = camera_config_status()["params"]
    merged = dict(current)
    for key, value in preset.items():
        if key == "label":
            continue
        merged[key] = value
    res = action_camera_config_apply(merged, restart=restart)
    res["preset"] = preset_id
    res["preset_label"] = preset["label"]
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


def prepare_scan_camera():
    params = camera_config_status()["params"]
    params.update({
        "ExposureAutoString": "Once",
        "AutoExposureTimeLowerLimit": 100,
        "GainAuto": 0,
    })
    params, _ = normalize_camera_params(params)
    write_camera_config_yaml(CAMERA_CONFIG_PATH, params)
    stopped = action_camera_stop()
    started = action_camera_start()
    return {"ok": bool(started.get("ok")), "stop": stopped, "start": started, "params": params}


def wait_for_topic_message(container_name, topic, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline and container_name not in docker_all_names():
        time.sleep(0.5)
    if container_name not in docker_all_names():
        return {"ok": False, "output": f"容器未在{timeout}秒内启动: {container_name}"}
    last = {"ok": False, "output": f"等待{topic}"}
    while time.time() < deadline:
        last = run_cmd(
            docker_exec_ros_cmd(container_name, f"timeout 3 rostopic echo -n 1 {shlex.quote(topic)} >/dev/null"),
            timeout=5,
            cwd=DEPLOY_DIR,
        )
        if last.get("ok"):
            return last
        time.sleep(0.5)
    return last


def wait_for_lidar_ready():
    lidar = wait_for_topic_message("mid360_driver", "/livox/lidar", timeout=20)
    imu = wait_for_topic_message("mid360_driver", "/livox/imu", timeout=10) if lidar.get("ok") else {"ok": False, "output": "等待雷达失败"}
    return {"ok": bool(lidar.get("ok") and imu.get("ok")), "lidar": lidar, "imu": imu}


def wait_for_camera_ready(retry=True):
    image = wait_for_topic_message("mid360_driver", "/left_camera/image", timeout=25)
    if image.get("ok") or not retry:
        return image
    if not container_running(CONTAINERS["camera"]):
        action_camera_start()
        image = wait_for_topic_message("mid360_driver", "/left_camera/image", timeout=25)
    return image


def active_workflow():
    offline = read_offline_job()
    if read_active_recording():
        return "recording"
    if offline.get("status") in ("starting", "running", "draining", "saving", "cancel_requested"):
        return "offline_mapping"
    if container_running(CONTAINERS["fusion"]) or read_active_fastlivo_scan():
        return "realtime_mapping"
    if container_running(CONTAINERS["gs_bag"]):
        return "recording"
    return "idle"


def record_runtime_status():
    active = read_active_recording()
    if not active:
        return {"active": False}
    bag = pathlib.Path(active.get("bag", ""))
    started_epoch = float(active.get("started_epoch") or time.time())
    free = filesystem_free_bytes()
    size = bag.stat().st_size if bag.is_file() else 0
    elapsed = max(0.0, time.time() - started_epoch)
    byte_rate = size / elapsed if elapsed > 1 else 0
    return {
        "active": True,
        "scan_id": active.get("scan_id"),
        "bag": str(bag),
        "size": size,
        "elapsed": round(elapsed, 1),
        "free_bytes": free,
        "warning": free < RECORD_WARN_FREE,
        "estimated_seconds_left": round(max(0, free - RECORD_AUTO_STOP_FREE) / byte_rate) if byte_rate > 0 else None,
    }


def recording_watchdog(scan_id):
    while True:
        time.sleep(2)
        active = read_active_recording()
        if active.get("scan_id") != scan_id:
            return
        if not container_running(CONTAINERS["gs_bag"]) and time.time() - float(active.get("started_epoch") or 0) > 15:
            action_fastlivo_record_stop(reason="recorder_exited")
            return
        if filesystem_free_bytes() <= RECORD_AUTO_STOP_FREE:
            action_fastlivo_record_stop(reason="disk_low_auto_stop")
            return


def action_fastlivo_record_start():
    with WORKFLOW_LOCK:
        mode = active_workflow()
        if mode != "idle":
            return {"ok": False, "message": f"当前正在执行 {mode}，不能开始录制"}
        free = filesystem_free_bytes()
        if free < RECORD_MIN_START_FREE:
            return {"ok": False, "message": f"磁盘可用空间不足15 GiB，当前 {free / GIB:.1f} GiB"}
        ensure_dirs()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        scan_dir = FASTLIVO_MAP_ROOT / stamp
        scan_dir.mkdir(parents=True, exist_ok=False)
        bag = scan_dir / f"{stamp}-gs-raw.bag"
        update_scan_workflow(scan_dir, "recording", {
            "status": "starting", "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "bag": str(bag),
        })
        action_lio_stop()
        lidar = action_lidar_start()
        lidar_ready = wait_for_lidar_ready() if lidar.get("ok") else {"ok": False}
        camera = prepare_scan_camera()
        camera_ready = wait_for_camera_ready() if camera.get("ok") and lidar_ready.get("ok") else {"ok": False}
        if not lidar.get("ok") or not lidar_ready.get("ok") or not camera.get("ok") or not camera_ready.get("ok"):
            action_stop_scan_runtime()
            update_scan_workflow(scan_dir, "recording", {"status": "invalid", "error": "相机或雷达启动失败"})
            return {"ok": False, "message": "相机或雷达未能稳定出帧", "lidar": lidar, "lidar_ready": lidar_ready, "camera": camera, "camera_ready": camera_ready}
        inner = (
            "rosbag record --lz4 --buffsize=1024 "
            f"-O {shlex.quote(str(bag))} "
            "/left_camera/image /livox/lidar /livox/imu"
        )
        cmd = named_ros_env_cmd("fast_livo2_gs_raw_bag_record", inner)
        bag_res = start_process("record-data", cmd, cwd=DEPLOY_DIR)
        active = {
            "scan_id": stamp, "scan_dir": str(scan_dir), "bag": str(bag),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "started_epoch": time.time(),
            "log": bag_res.get("log"),
        }
        write_active_recording(active)
        update_scan_workflow(scan_dir, "recording", {"status": "recording", "log": bag_res.get("log")})
        threading.Thread(target=recording_watchdog, args=(stamp,), daemon=True).start()
        return {"ok": True, "scan_id": stamp, "scan_dir": str(scan_dir), "bag": str(bag), "lidar": lidar, "lidar_ready": lidar_ready, "camera": camera, "camera_ready": camera_ready}


def action_fastlivo_record_stop(reason="operator"):
    with WORKFLOW_LOCK:
        active = read_active_recording()
        if not active:
            return {"ok": False, "message": "当前没有正在录制的数据"}
        scan_dir = pathlib.Path(active["scan_dir"])
        update_scan_workflow(scan_dir, "recording", {"status": "stopping", "stop_reason": reason})
        stop_bag = docker_sigint_wait("fast_livo2_gs_raw_bag_record", timeout=45)
        stop_devices = action_stop_scan_runtime()
        bag_info = inspect_rosbag(active["bag"])
        status = "valid" if stop_bag.get("ok") and bag_info.get("valid") else "invalid"
        update_scan_workflow(scan_dir, "recording", {
            "status": status,
            "stopped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "stop_reason": reason,
            "bag_info": {key: value for key, value in bag_info.items() if key != "output"},
            "error": "; ".join(bag_info.get("errors") or []),
        })
        clear_active_recording()
        return {
            "ok": status == "valid", "scan_id": scan_dir.name, "scan_dir": str(scan_dir),
            "bag_info": bag_info, "stop_bag": stop_bag, "stop_devices": stop_devices,
            "message": "录制完成，数据校验通过" if status == "valid" else "录制已停止，但数据校验未通过",
        }


def container_state(name):
    res = run_cmd(["docker", "inspect", "-f", "{{.State.Status}}|{{.State.ExitCode}}", name], timeout=5, cwd=DEPLOY_DIR)
    if not res.get("ok"):
        return {"exists": False, "status": "missing", "exit_code": None}
    parts = res.get("output", "").strip().split("|", 1)
    try:
        exit_code = int(parts[1]) if len(parts) > 1 else None
    except ValueError:
        exit_code = None
    return {"exists": True, "status": parts[0], "exit_code": exit_code}


def set_container_paused(name, paused):
    return run_cmd(["docker", "pause" if paused else "unpause", name], timeout=8, cwd=DEPLOY_DIR)


def stop_offline_runtime():
    state = container_state("fast_livo2_offline_bag_play")
    if state.get("status") == "paused":
        set_container_paused("fast_livo2_offline_bag_play", False)
    player = docker_sigint_wait("fast_livo2_offline_bag_play", timeout=15)
    mapper = docker_sigint_wait("fast_livo2_mapping", timeout=90)
    docker_rm(["fast_livo2_offline_bag_play"])
    return {"player": player, "mapper": mapper}


def validate_fastlivo_outputs(final_status=None):
    raw = FASTLIVO_PCD_DIR / "all_raw_points.pcd"
    poses = FASTLIVO_LOG_DIR / "image" / "image_poses.txt"
    images = list((FASTLIVO_LOG_DIR / "image").glob("*.png"))
    errors = []
    if not raw.is_file() or raw.stat().st_size < 1024:
        errors.append("all_raw_points.pcd缺失或为空")
    pose_count = 0
    if poses.is_file():
        pose_count = sum(1 for line in poses.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())
    if pose_count <= 0:
        errors.append("image_poses.txt为空")
    if not images:
        errors.append("没有保存训练图像")
    dropped = int((final_status or {}).get("image_save_dropped") or 0)
    if dropped:
        errors.append(f"图片保存丢弃{dropped}帧")
    return {
        "ok": not errors, "errors": errors, "raw_pcd_size": raw.stat().st_size if raw.is_file() else 0,
        "pose_count": pose_count, "image_count": len(images), "image_save_dropped": dropped,
    }


def promote_offline_outputs(scan_dir, raw_bag, logs, job_id):
    scan_dir = pathlib.Path(scan_dir)
    backup = scan_dir.parent / f".{scan_dir.name}-backup-{job_id}"
    if backup.exists():
        shutil.rmtree(backup)
    backup.mkdir(parents=True)
    names = ("all_raw_points.pcd", "all_downsampled_points.pcd", "lidar_poses.txt", "image_poses.txt", "images", "calib", "colmap", "metadata.json")
    moved = []
    try:
        for name in names:
            source = scan_dir / name
            if source.exists():
                shutil.move(str(source), str(backup / name))
                moved.append(name)
        saved = copy_fastlivo_outputs(scan_dir, extra_logs=logs, raw_bag=raw_bag)
        archive = saved.get("dataset_archive") or {}
        if saved.get("missing") or not archive.get("ok"):
            raise RuntimeError("输出归档验证失败: " + "; ".join((saved.get("missing") or []) + (archive.get("missing") or []) + (archive.get("errors") or [])))
        previous_metadata = read_json_file(backup / "metadata.json")
        current_metadata = read_json_file(scan_dir / "metadata.json")
        if previous_metadata.get("workflow"):
            current_metadata["workflow"] = previous_metadata["workflow"]
            atomic_write_json(scan_dir / "metadata.json", current_metadata)
        shutil.rmtree(backup, ignore_errors=True)
        return {"ok": True, "saved": saved}
    except Exception as exc:
        for name in names:
            current = scan_dir / name
            safe_remove_path(current, scan_dir)
        for name in moved:
            old = backup / name
            if old.exists():
                shutil.move(str(old), str(scan_dir / name))
        shutil.rmtree(backup, ignore_errors=True)
        return {"ok": False, "error": str(exc)}


def offline_job_update(job, **values):
    persisted = read_offline_job()
    requested_status = values.get("status")
    if persisted.get("job_id") == job.get("job_id") and persisted.get("status") == "cancel_requested" and requested_status in ("starting", "running", "draining", "saving"):
        values["status"] = "cancel_requested"
    job.update(values)
    job["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    write_offline_job(job)
    scan_dir = pathlib.Path(job["scan_dir"])
    update_scan_workflow(scan_dir, "offline", {
        key: value for key, value in job.items()
        if key in ("job_id", "status", "progress", "lag", "paused", "started_at", "updated_at", "completed_at", "error", "validation")
    })


def offline_mapping_worker(job):
    player_name = "fast_livo2_offline_bag_play"
    mapper_log = None
    player_log = None
    paused = False
    final_status = {}
    try:
        scan_dir = pathlib.Path(job["scan_dir"])
        bag = pathlib.Path(job["bag"])
        bag_info = job["bag_info"]
        clear_fastlivo_log_outputs()
        mapper_cmd = named_ros_env_cmd(
            "fast_livo2_mapping",
            "roslaunch jr_fastlivo_validation fast_livo2_saved_mapping.launch "
            "rviz:=false pcd_save_en:=true pcd_save_type:=0 pcd_save_interval:=-1 pcd_filter_size:=0.15 "
            "img_save_en:=true img_save_interval:=1 pose_output_en:=true colmap_output_en:=true",
        )
        mapper = start_process("fastlivo-offline", mapper_cmd, cwd=DEPLOY_DIR)
        mapper_log = mapper.get("log")
        offline_job_update(job, status="starting", progress=0.0, mapper_log=mapper_log)
        deadline = time.time() + 30
        while time.time() < deadline and "fast_livo2_mapping" not in {row["name"] for row in docker_ps()}:
            time.sleep(1)
        if "fast_livo2_mapping" not in {row["name"] for row in docker_ps()}:
            raise RuntimeError("FAST-LIVO2离线容器启动失败")
        ready_deadline = time.time() + 45
        subscribers_ready = False
        while time.time() < ready_deadline:
            node_info = run_cmd(
                docker_exec_ros_cmd("fast_livo2_mapping", "timeout 3 rosnode info /laserMapping 2>/dev/null || true"),
                timeout=5, cwd=DEPLOY_DIR,
            ).get("output", "")
            bridge_info = run_cmd(
                docker_exec_ros_cmd("fast_livo2_mapping", "timeout 3 rosnode info /livox_driver2_to_legacy 2>/dev/null || true"),
                timeout=5, cwd=DEPLOY_DIR,
            ).get("output", "")
            if "/left_camera/image" in node_info and "/livox/imu" in node_info and "/livox/lidar" in bridge_info:
                subscribers_ready = True
                break
            time.sleep(1)
        if not subscribers_ready:
            raise RuntimeError("FAST-LIVO2订阅器在45秒内未就绪")
        docker_rm([player_name])
        play_inner = f"rosbag play --rate 0.5 {shlex.quote(str(bag))}"
        player_cmd = named_ros_env_cmd(player_name, play_inner, remove=False)
        player = start_process("fastlivo-offline-play", player_cmd, cwd=DEPLOY_DIR)
        player_log = player.get("log")
        offline_job_update(job, status="running", player_log=player_log, paused=False)
        deadline = time.time() + 30
        while time.time() < deadline and not container_state(player_name).get("exists"):
            time.sleep(0.5)
        if not container_state(player_name).get("exists"):
            raise RuntimeError("rosbag离线回放容器启动失败")
        bag_start = float(bag_info.get("start") or 0)
        bag_end = float(bag_info.get("end") or 0)
        duration = max(0.001, bag_end - bag_start)
        last_progress_time = time.time()
        last_processed = -1.0
        while True:
            persisted = read_offline_job()
            if persisted.get("job_id") != job["job_id"] or persisted.get("status") == "cancel_requested":
                raise InterruptedError("operator cancelled")
            state = container_state(player_name)
            if state.get("status") in ("created", "restarting"):
                time.sleep(0.5)
                continue
            if state.get("status") not in ("running", "paused"):
                if state.get("exit_code") not in (0, None):
                    raise RuntimeError(f"rosbag play退出码 {state.get('exit_code')}")
                break
            status = fastlivo_processing_status()
            if status.get("ok"):
                final_status = status
                processed = float(status.get("last_processed") or 0)
                progress = max(0.0, min(0.995, (processed - bag_start) / duration)) if processed > 0 else 0.0
                if processed > last_processed + 0.01:
                    last_processed = processed
                    last_progress_time = time.time()
                lag = float(status.get("lag") or 0)
                if not paused and lag > 2.0:
                    set_container_paused(player_name, True)
                    paused = True
                elif paused and lag <= 0.5:
                    set_container_paused(player_name, False)
                    paused = False
                offline_job_update(job, status="running", progress=round(progress, 4), lag=round(lag, 3), paused=paused, processing=status)
            if time.time() - last_progress_time > 180:
                raise RuntimeError("离线建图连续180秒没有处理进展")
            time.sleep(1)
        docker_rm([player_name])
        offline_job_update(job, status="draining", paused=False)
        ready = 2 if final_status.get("ok") and float(final_status.get("lag") or 0) <= 0.5 else 0
        drain_deadline = time.time() + max(600, float(bag_info.get("duration") or 0) * 8)
        while ready < 2 and time.time() < drain_deadline:
            persisted = read_offline_job()
            if persisted.get("status") == "cancel_requested":
                raise InterruptedError("operator cancelled")
            status = fastlivo_processing_status()
            if status.get("ok"):
                final_status = status
                lag = float(status.get("lag") or 0)
                progress = max(0.0, min(1.0, (float(status.get("last_processed") or 0) - bag_start) / duration))
                ready = ready + 1 if lag <= 0.5 else 0
                offline_job_update(job, status="draining", progress=round(progress, 4), lag=round(lag, 3), processing=status)
                if ready >= 2:
                    break
            time.sleep(1)
        if ready < 2:
            raise RuntimeError("回放结束后FAST-LIVO2积压未能排空")
        offline_job_update(job, status="saving", progress=1.0, lag=float(final_status.get("lag") or 0))
        stop_mapper = docker_sigint_wait("fast_livo2_mapping", timeout=180)
        if not stop_mapper.get("ok"):
            raise RuntimeError(stop_mapper.get("output") or "FAST-LIVO2未能正常保存")
        validation = validate_fastlivo_outputs(final_status)
        if not validation.get("ok"):
            raise RuntimeError("; ".join(validation.get("errors") or []))
        promotion = promote_offline_outputs(scan_dir, bag, [path for path in (mapper_log, player_log) if path], job["job_id"])
        if not promotion.get("ok"):
            raise RuntimeError(promotion.get("error") or "结果替换失败")
        offline_job_update(
            job, status="completed", progress=1.0, paused=False, validation=validation,
            completed_at=time.strftime("%Y-%m-%d %H:%M:%S"), result_file="all_raw_points.pcd",
        )
    except InterruptedError:
        stop_offline_runtime()
        offline_job_update(job, status="cancelled", paused=False, error="用户停止了离线建图")
    except Exception as exc:
        stop_offline_runtime()
        offline_job_update(job, status="failed", paused=False, error=str(exc))


def action_fastlivo_offline_start(scan_id):
    with WORKFLOW_LOCK:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", scan_id or ""):
            return {"ok": False, "message": "扫描ID不合法"}
        if active_workflow() != "idle":
            return {"ok": False, "message": f"当前正在执行 {active_workflow()}，不能开始离线建图"}
        scan_dir = (FASTLIVO_MAP_ROOT / scan_id).resolve()
        if scan_dir.parent != FASTLIVO_MAP_ROOT.resolve() or not scan_dir.is_dir():
            return {"ok": False, "message": "扫描记录不存在"}
        bag = find_scan_bag(scan_dir)
        if not bag:
            return {"ok": False, "message": "扫描记录缺少原始bag"}
        bag_info = inspect_rosbag(bag)
        if not bag_info.get("valid"):
            return {"ok": False, "message": "原始bag校验未通过", "bag_info": bag_info}
        update_scan_workflow(scan_dir, "recording", {
            "status": "valid",
            "bag": str(bag),
            "bag_info": {key: value for key, value in bag_info.items() if key != "output"},
        })
        required_free = max(10 * GIB, int(bag.stat().st_size * 0.5))
        if filesystem_free_bytes() < required_free:
            return {"ok": False, "message": f"离线建图空间不足，需要至少 {required_free / GIB:.1f} GiB"}
        job = {
            "job_id": uuid.uuid4().hex[:12], "scan_id": scan_id, "scan_dir": str(scan_dir), "bag": str(bag),
            "bag_info": {key: value for key, value in bag_info.items() if key != "output"},
            "status": "starting", "progress": 0.0, "paused": False,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        write_offline_job(job)
        update_scan_workflow(scan_dir, "offline", {"job_id": job["job_id"], "status": "starting", "progress": 0.0, "started_at": job["started_at"]})
        threading.Thread(target=offline_mapping_worker, args=(job,), daemon=True).start()
        return {"ok": True, "job": job}


def action_fastlivo_offline_cancel():
    with WORKFLOW_LOCK:
        job = read_offline_job()
        if job.get("status") not in ("starting", "running", "draining", "saving"):
            return {"ok": False, "message": "当前没有可停止的离线建图任务", "job": job}
        job["status"] = "cancel_requested"
        write_offline_job(job)
        update_scan_workflow(job["scan_dir"], "offline", {"status": "cancel_requested"})
        return {"ok": True, "message": "已请求停止离线建图", "job": job}


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
    if active_workflow() == "offline_mapping":
        return {"ok": False, "message": "当前是离线建图任务，请使用停止离线建图"}
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
    raw_bag_stop = docker_sigint_wait("fast_livo2_gs_raw_bag_record", timeout=35)
    catch_up = wait_fastlivo_catch_up(max_wait=30, target_lag=0.5)
    stop_res = docker_sigint_wait("fast_livo2_mapping", timeout=90)
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
        "processing catch-up:",
        catch_up.get("output", ""),
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
        "processing_catch_up": catch_up,
        "save": saved,
        "stop_runtime": runtime_stop,
        "output": "\n".join(str(x) for x in output if x),
    }


def action_fastlivo_start_all():
    mode = active_workflow()
    if mode not in ("idle", "realtime_mapping"):
        return {"ok": False, "message": f"当前正在执行 {mode}，不能开始实时建图"}
    lio_stop = action_lio_stop()
    lidar = action_lidar_start()
    lidar_ready = wait_for_lidar_ready() if lidar.get("ok") else {"ok": False}
    camera_result = prepare_scan_camera()
    camera_stop = camera_result.get("stop")
    camera = camera_result.get("start")
    camera_ready = wait_for_camera_ready() if camera_result.get("ok") and lidar_ready.get("ok") else {"ok": False}
    if not lidar_ready.get("ok") or not camera_ready.get("ok"):
        action_stop_scan_runtime()
        return {
            "ok": False, "message": "相机或雷达未能稳定出帧，实时建图未启动",
            "lio_stop": lio_stop, "lidar": lidar, "lidar_ready": lidar_ready,
            "camera_stop": camera_stop, "camera": camera, "camera_ready": camera_ready,
        }
    mapping = action_fastlivo_start()
    return {
        "ok": bool(lidar.get("ok") and camera.get("ok") and mapping.get("ok")),
        "lio_stop": lio_stop,
        "lidar": lidar,
        "lidar_ready": lidar_ready,
        "camera_stop": camera_stop,
        "camera": camera,
        "camera_ready": camera_ready,
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
    mode = active_workflow()
    if mode == "recording":
        stopped = action_fastlivo_record_stop(reason="stop_all")
        return {"ok": bool(stopped.get("ok")), "stop_recording": stopped, "output": stopped.get("message", "")}
    if mode == "offline_mapping":
        cancel = action_fastlivo_offline_cancel()
        return {"ok": bool(cancel.get("ok")), "cancel_offline": cancel, "output": cancel.get("message", "")}
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


async def stream_camera(writer, quality, profile="default"):
    ensure_dirs()
    safe_quality = quality if quality in ("mini", "pc") else "mini"
    # The native camera stream is 2448x2048. Downscale only the browser preview
    # to control JPEG/WebSocket load; ROS mapping and saved images stay native.
    if profile == "recording":
        width, hz, jpeg_quality = "960", "8", "75"
    else:
        width = "1280"
        hz = "5" if safe_quality == "mini" else "8"
        jpeg_quality = "78" if safe_quality == "mini" else "84"
    inner = (
        "python3 /home/jr/fast_livo2_data/tools/ros_image_stream.py "
        f"--topics /left_camera/image,/rgb_img --hz {hz} --width {width} --quality {jpeg_quality}"
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
    profile = params.get("profile", ["default"])[0]
    if kind == "camera":
        await stream_camera(writer, quality, profile)
    else:
        await stream_points(writer, mode, quality)


async def read_request_body(reader, headers):
    try:
        length = int(headers.get("content-length", "0") or "0")
    except Exception:
        length = 0
    if length <= 0:
        return b""
    length = min(length, 256 * 1024)
    return await reader.readexactly(length)


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

    body = b""
    if method in ("POST", "PUT", "PATCH"):
        try:
            body = await read_request_body(reader, headers)
        except Exception:
            body = b""

    def parse_json_body():
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return None

    try:
        if clean_path == "/api/status" and method == "GET":
            json_response(writer, api_status())
        elif clean_path == "/api/fastlivo/maps" and method == "GET":
            json_response(writer, list_fastlivo_maps())
        elif clean_path == "/api/fastlivo/scans" and method == "GET":
            json_response(writer, list_fastlivo_scans())
        elif clean_path == "/api/fastlivo/offline/status" and method == "GET":
            json_response(writer, {"ok": True, "job": read_offline_job()})
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
        elif method == "GET" and clean_path == "/api/camera/config":
            json_response(writer, camera_config_status())
        elif method == "POST" and clean_path == "/api/camera/config":
            payload = parse_json_body()
            if payload is None:
                json_response(writer, {"ok": False, "output": "invalid JSON body"})
            else:
                restart = bool(payload.get("restart", True))
                params = payload.get("params") if isinstance(payload.get("params"), dict) else payload
                json_response(writer, action_camera_config_apply(params, restart=restart))
        elif method == "POST" and clean_path == "/api/camera/preset":
            payload = parse_json_body() or {}
            preset_id = str(payload.get("preset") or payload.get("id") or "").strip()
            restart = bool(payload.get("restart", True))
            json_response(writer, action_camera_preset(preset_id, restart=restart))
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
        elif method == "POST" and clean_path == "/api/fastlivo/record/start":
            json_response(writer, action_fastlivo_record_start())
        elif method == "POST" and clean_path == "/api/fastlivo/record/stop":
            json_response(writer, action_fastlivo_record_stop())
        elif method == "POST" and clean_path == "/api/fastlivo/offline/start":
            payload = parse_json_body() or {}
            json_response(writer, action_fastlivo_offline_start(str(payload.get("scan_id") or "")))
        elif method == "POST" and clean_path == "/api/fastlivo/offline/cancel":
            json_response(writer, action_fastlivo_offline_cancel())
        elif method == "POST" and clean_path == "/api/fastlivo/scans/delete":
            payload = parse_json_body() or {}
            json_response(writer, action_fastlivo_scan_delete(str(payload.get("scan_id") or "")))
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
    job = read_offline_job()
    if job.get("status") in ("starting", "running", "draining", "saving", "cancel_requested"):
        job.update({
            "status": "failed",
            "error": "控制台服务重启，离线任务已安全中断，可重新执行",
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        write_offline_job(job)
        if job.get("scan_dir"):
            update_scan_workflow(job["scan_dir"], "offline", {"status": "failed", "error": job["error"], "updated_at": job["updated_at"]})
        threading.Thread(target=stop_offline_runtime, daemon=True).start()
    elif container_running(CONTAINERS["offline_play"]):
        threading.Thread(target=stop_offline_runtime, daemon=True).start()
    if read_active_recording():
        threading.Thread(target=action_fastlivo_record_stop, kwargs={"reason": "service_restarted"}, daemon=True).start()
    server = await asyncio.start_server(handle_http, HOST, PORT)
    print(f"JR scanner console listening on http://{HOST}:{PORT}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
