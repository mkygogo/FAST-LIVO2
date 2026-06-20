#!/usr/bin/env python3
import asyncio
import base64
import hashlib
import json
import os
import pathlib
import re
import signal
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
HOST = "127.0.0.1"
PORT = int(os.environ.get("FAST_LIVO2_CONSOLE_PORT", "8090"))


CONTAINERS = {
    "lidar": ["mid360_driver", "mid360_preview_driver", "mid360_driver_test"],
    "lio": ["jr_lidar_mapping"],
    "fusion": ["fast_livo2_mapping"],
    "bag": ["fast_livo2_bag_record"],
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def ensure_dirs():
    for path in (OUTPUT_DIR, TOOLS_DIR, BAGS_DIR, LOG_DIR):
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
        (name for name in CONTAINERS["lio"] + CONTAINERS["fusion"] + CONTAINERS["lidar"] + CONTAINERS["bag"] if name in current_names),
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
    return docker_rm(CONTAINERS["lidar"])


def action_lidar_check():
    script = DEPLOY_DIR / "check_mid360.sh"
    res = run_cmd([str(script)], timeout=20, cwd=DEPLOY_DIR)
    path = log_path("lidar-check")
    path.write_text(res["output"], encoding="utf-8", errors="replace")
    res["log"] = str(path)
    return res


def action_fastlivo_start():
    running = container_running(CONTAINERS["fusion"])
    if running:
        return {"ok": True, "message": "JR扫描仪融合算法已在运行", "running": running}
    cmd = named_ros_env_cmd(
        "fast_livo2_mapping",
        "roslaunch fast_livo mapping_avia.launch rviz:=false",
    )
    return start_process("fastlivo", cmd, cwd=DEPLOY_DIR)


def action_fastlivo_stop():
    return docker_rm(CONTAINERS["fusion"])


def action_fastlivo_start_all():
    lidar = action_lidar_start()
    time.sleep(1)
    mapping = action_fastlivo_start()
    return {"ok": bool(lidar.get("ok") and mapping.get("ok")), "lidar": lidar, "mapping": mapping}


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
    names = CONTAINERS["lidar"] + CONTAINERS["lio"] + CONTAINERS["fusion"] + CONTAINERS["bag"]
    stopped = docker_rm(names)
    sleep_script = DEPLOY_DIR / "livox_sleep.sh"
    if sleep_script.exists():
        sleep_res = run_cmd([str(sleep_script)], timeout=25, cwd=DEPLOY_DIR)
    else:
        sleep_res = {"ok": False, "output": "livox_sleep.sh not installed"}
    return {
        "ok": bool(stopped.get("ok")),
        "stop_processes": stopped,
        "sleep_lidar": sleep_res,
        "output": (stopped.get("output", "") + "\n" + sleep_res.get("output", "")).strip(),
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


async def stream_points(writer, mode):
    ensure_dirs()
    safe_mode = mode if mode in ("lidar", "mapping") else "lidar"
    max_points = "12000" if safe_mode == "lidar" else "22000"
    hz = "4" if safe_mode == "lidar" else "3"
    inner = (
        f"python3 /home/jr/fast_livo2_data/tools/ros_point_stream.py "
        f"--mode {safe_mode} --max-points {max_points} --hz {hz}"
    )
    current = {row["name"] for row in docker_ps()}
    candidates = CONTAINERS["lidar"] if safe_mode == "lidar" else CONTAINERS["lio"] + CONTAINERS["fusion"] + CONTAINERS["lidar"]
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


async def handle_websocket(reader, writer, path, headers):
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
    await stream_points(writer, mode)


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

    if headers.get("upgrade", "").lower() == "websocket" and clean_path == "/ws/points":
        await handle_websocket(reader, writer, path, headers)
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError):
            pass
        return

    try:
        if clean_path == "/api/status" and method == "GET":
            json_response(writer, api_status())
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
        elif method == "POST" and clean_path == "/api/fastlivo/start":
            json_response(writer, action_fastlivo_start())
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
