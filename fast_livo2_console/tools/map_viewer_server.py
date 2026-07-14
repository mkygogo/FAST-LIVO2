#!/usr/bin/env python3
import argparse
import html
import json
import mimetypes
import pathlib
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ALLOWED_MAP_SUFFIXES = {
    ".pcd",
    ".ply",
    ".json",
    ".yaml",
    ".yml",
    ".txt",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
}


class MapViewerHandler(BaseHTTPRequestHandler):
    root = pathlib.Path.home() / "fast_livo2_data" / "output" / "fast_livo2_maps"
    deploy_dir = pathlib.Path(__file__).resolve().parent
    repo_static = deploy_dir.parent / "static"
    viewer = deploy_dir / "map_viewer.html" if (deploy_dir / "map_viewer.html").exists() else repo_static / "map_viewer.html"
    vendor = deploy_dir / "vendor" if (deploy_dir / "vendor").exists() else repo_static / "vendor"
    pack_builder = None

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def send_bytes(self, body, content_type="application/octet-stream", status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj):
        self.send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def safe_map_file(self, rel):
        rel_path = pathlib.PurePosixPath(urllib.parse.unquote(rel).lstrip("/"))
        if ".." in rel_path.parts:
            return None
        path = (self.root / pathlib.Path(*rel_path.parts)).resolve()
        root = self.root.resolve()
        if root == path or root not in path.parents:
            return None
        if not path.is_file() or path.suffix.lower() not in ALLOWED_MAP_SUFFIXES:
            return None
        return path

    def safe_vendor_file(self, rel):
        rel_path = pathlib.PurePosixPath(urllib.parse.unquote(rel).lstrip("/"))
        path = (self.vendor / pathlib.Path(*rel_path.parts)).resolve()
        root = self.vendor.resolve()
        if root == path or root not in path.parents:
            return None
        if not path.is_file() or path.suffix.lower() not in (".js", ".mjs", ".css"):
            return None
        return path

    def resolve_pack_builder(self):
        if self.pack_builder and pathlib.Path(self.pack_builder).exists():
            return pathlib.Path(self.pack_builder)
        home = pathlib.Path.home()
        candidates = [
            home / "fast_livo2_data" / "tools" / "build_replay_pack.py",
            home / "fast_livo2_deploy" / "console" / "tools" / "build_replay_pack.py",
            self.deploy_dir / "build_replay_pack.py",
            self.deploy_dir.parent / "tools" / "build_replay_pack.py",
        ]
        for path in candidates:
            if path.exists():
                return path
        return None

    def safe_scan_dir(self, scan_id):
        scan_id = (scan_id or "").strip().strip("/")
        if not scan_id or "/" in scan_id or ".." in scan_id:
            return None
        path = (self.root / scan_id).resolve()
        root = self.root.resolve()
        if root not in path.parents and path != root:
            return None
        if not path.is_dir():
            return None
        return path

    def replay_info_for(self, folder):
        traj = folder / "replay" / "trajectory.json"
        manifest = folder / "replay" / "manifest.json"
        has_replay = traj.exists()
        info = {
            "ok": has_replay,
            "trajectory": str(traj.relative_to(self.root)).replace("\\", "/") if has_replay else None,
            "path_count": None,
            "frame_count": None,
            "duration": None,
        }
        if has_replay:
            try:
                data = json.loads(traj.read_text(encoding="utf-8"))
                info["path_count"] = len(data.get("path") or [])
                info["frame_count"] = len(data.get("frames") or [])
                info["duration"] = data.get("duration")
            except Exception:
                pass
        if manifest.exists() and info["path_count"] is None:
            try:
                man = json.loads(manifest.read_text(encoding="utf-8"))
                info["path_count"] = man.get("path_count")
                info["frame_count"] = man.get("frame_count")
                info["duration"] = man.get("duration")
            except Exception:
                pass
        return has_replay, info

    def list_maps(self):
        maps = []
        if self.root.exists():
            # Sort by scan id (YYYYMMDD-HHMMSS) newest first — not mtime, which
            # changes when replay packs or metadata are rewritten.
            for folder in sorted((p for p in self.root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
                files = []
                for file in sorted(folder.iterdir()):
                    if file.is_file() and file.suffix.lower() in (".pcd", ".ply"):
                        stat = file.stat()
                        files.append({
                            "name": file.name,
                            "path": str(file.relative_to(self.root)).replace("\\", "/"),
                            "size": stat.st_size,
                            "mtime": int(stat.st_mtime),
                        })
                has_replay, replay_info = self.replay_info_for(folder)
                maps.append({
                    "name": folder.name,
                    "path": str(folder.relative_to(self.root)).replace("\\", "/"),
                    "files": files,
                    "has_replay": has_replay,
                    "replay": replay_info,
                })
        return maps

    def build_replay(self, scan_id):
        scan_dir = self.safe_scan_dir(scan_id)
        if not scan_dir:
            return {"ok": False, "error": f"invalid scan: {scan_id}"}
        builder = self.resolve_pack_builder()
        if not builder:
            return {"ok": False, "error": "build_replay_pack.py not found"}
        home = pathlib.Path.home()
        args = [
            "python3",
            str(builder),
            "--scan-dir",
            str(scan_dir),
            "--fastlivo-log",
            str(home / "fast_livo2_ws" / "src" / "FAST-LIVO2" / "Log"),
            "--gs-root",
            str(home / "fast_livo2_data" / "output" / "gs_livo_datasets"),
        ]
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "replay pack timed out (180s)", "scan_id": scan_dir.name}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "scan_id": scan_dir.name}

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        payload = None
        if out:
            try:
                payload = json.loads(out.splitlines()[-1])
            except Exception:
                payload = None
        has_replay, replay_info = self.replay_info_for(scan_dir)
        ok = bool(payload.get("ok") if isinstance(payload, dict) else has_replay)
        return {
            "ok": ok,
            "scan_id": scan_dir.name,
            "has_replay": has_replay,
            "replay": replay_info,
            "builder": str(builder),
            "code": proc.returncode,
            "result": payload,
            "output": out[-2000:],
            "stderr": err[-1000:],
        }

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except Exception:
            length = 0
        if length <= 0:
            return {}
        length = min(length, 64 * 1024)
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/viewer"):
            if not self.viewer.exists():
                self.send_bytes(b"viewer missing", "text/plain; charset=utf-8", 500)
                return
            self.send_bytes(self.viewer.read_bytes(), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/maps":
            self.send_json({"ok": True, "root": str(self.root), "maps": self.list_maps()})
            return
        if parsed.path.startswith("/files/"):
            file = self.safe_map_file(parsed.path[len("/files/"):])
            if not file:
                self.send_bytes(b"not found", "text/plain; charset=utf-8", 404)
                return
            ctype = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
            if file.suffix.lower() in (".jpg", ".jpeg"):
                ctype = "image/jpeg"
            elif file.suffix.lower() == ".png":
                ctype = "image/png"
            elif file.suffix.lower() == ".json":
                ctype = "application/json; charset=utf-8"
            self.send_bytes(file.read_bytes(), ctype)
            return
        if parsed.path.startswith("/vendor/"):
            file = self.safe_vendor_file(parsed.path[len("/vendor/"):])
            if not file:
                self.send_bytes(b"not found", "text/plain; charset=utf-8", 404)
                return
            ctype = mimetypes.guess_type(str(file))[0] or "application/javascript; charset=utf-8"
            self.send_bytes(file.read_bytes(), ctype)
            return
        if parsed.path == "/health":
            self.send_json({"ok": True})
            return
        self.send_bytes(("not found: " + html.escape(parsed.path)).encode("utf-8"), "text/plain; charset=utf-8", 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/replay/build":
            body = self.read_json_body()
            if body is None:
                self.send_json({"ok": False, "error": "invalid JSON body"})
                return
            scan_id = str(body.get("scan_id") or body.get("id") or body.get("path") or "").strip()
            self.send_json(self.build_replay(scan_id))
            return
        self.send_bytes(("not found: " + html.escape(parsed.path)).encode("utf-8"), "text/plain; charset=utf-8", 404)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18180)
    parser.add_argument("--root", default=str(MapViewerHandler.root))
    parser.add_argument("--viewer", default=str(MapViewerHandler.viewer))
    parser.add_argument("--vendor", default=str(MapViewerHandler.vendor))
    parser.add_argument("--pack-builder", default="")
    args = parser.parse_args()
    MapViewerHandler.root = pathlib.Path(args.root)
    MapViewerHandler.viewer = pathlib.Path(args.viewer)
    MapViewerHandler.vendor = pathlib.Path(args.vendor)
    if args.pack_builder:
        MapViewerHandler.pack_builder = pathlib.Path(args.pack_builder)
    httpd = ThreadingHTTPServer((args.host, args.port), MapViewerHandler)
    print(f"JR map viewer listening on http://{args.host}:{args.port}", flush=True)
    print(f"Serving maps from {MapViewerHandler.root}", flush=True)
    print(f"Serving viewer from {MapViewerHandler.viewer}", flush=True)
    print(f"Serving vendor from {MapViewerHandler.vendor}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
