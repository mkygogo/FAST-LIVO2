#!/usr/bin/env python3
"""Build a browser-friendly scan replay pack for the 18180 map viewer.

Reads SLAM poses + images (not raw IMU) and writes:
  <scan_dir>/replay/trajectory.json
  <scan_dir>/replay/images/*
  <scan_dir>/replay/manifest.json
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import re
import shutil
import sys
import time

IMAGE_EXTS = (".png", ".jpg", ".jpeg")


def parse_pose_file(path):
    poses = []
    path = pathlib.Path(path)
    if not path.exists():
        return poses
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = raw.strip().split()
        if len(parts) < 8:
            continue
        try:
            vals = [float(x) for x in parts[:8]]
        except ValueError:
            continue
        poses.append(
            {
                "timestamp": vals[0],
                "tx": vals[1],
                "ty": vals[2],
                "tz": vals[3],
                "qx": vals[4],
                "qy": vals[5],
                "qz": vals[6],
                "qw": vals[7],
            }
        )
    return poses


def image_timestamp(path):
    try:
        return float(path.stem)
    except ValueError:
        nums = re.findall(r"\d+(?:\.\d+)?", path.stem)
        return float(nums[-1]) if nums else math.inf


def pose_entry(p):
    return {
        "t": round(float(p["timestamp"]), 6),
        "p": [round(p["tx"], 6), round(p["ty"], 6), round(p["tz"], 6)],
        "q": [round(p["qx"], 6), round(p["qy"], 6), round(p["qz"], 6), round(p["qw"], 6)],
    }


def find_first_existing(candidates):
    for path in candidates:
        path = pathlib.Path(path)
        if path.exists():
            return path
    return None


def collect_images(dirs):
    files = []
    seen = set()
    for d in dirs:
        d = pathlib.Path(d)
        if not d.exists() or not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
                continue
            key = p.name
            if key in seen:
                continue
            seen.add(key)
            files.append(p)
    files.sort(key=image_timestamp)
    return files


def downsample_list(items, max_n):
    if max_n <= 0 or len(items) <= max_n:
        return items
    if max_n == 1:
        return [items[0]]
    step = (len(items) - 1) / float(max_n - 1)
    out = []
    used = set()
    for i in range(max_n):
        idx = int(round(i * step))
        idx = max(0, min(len(items) - 1, idx))
        if idx in used:
            continue
        used.add(idx)
        out.append(items[idx])
    return out


def convert_or_copy_image(src, dst, quality=80, width=0):
    dst = pathlib.Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    src = pathlib.Path(src)
    # Prefer JPEG output for smaller HTTP payload.
    if dst.suffix.lower() not in (".jpg", ".jpeg"):
        dst = dst.with_suffix(".jpg")
    try:
        from PIL import Image  # type: ignore

        with Image.open(src) as im:
            im = im.convert("RGB")
            if width and im.width > width:
                h = max(1, int(round(im.height * (width / float(im.width)))))
                im = im.resize((int(width), h), Image.BILINEAR)
            im.save(dst, format="JPEG", quality=int(quality), optimize=True)
        return dst
    except Exception:
        # Fallback: copy original extension if PIL missing or decode fails.
        if src.suffix.lower() in (".jpg", ".jpeg"):
            shutil.copy2(src, dst)
            return dst
        fallback = dst.with_suffix(src.suffix.lower())
        shutil.copy2(src, fallback)
        return fallback


def match_frames(image_poses, images, max_dt=0.08):
    if not image_poses or not images:
        return []
    frames = []
    j = 0
    for pose in image_poses:
        t = pose["timestamp"]
        while j + 1 < len(images) and abs(image_timestamp(images[j + 1]) - t) <= abs(image_timestamp(images[j]) - t):
            j += 1
        dt = abs(image_timestamp(images[j]) - t)
        if dt > max_dt:
            # search nearby window
            best_i = j
            best_dt = dt
            lo = max(0, j - 8)
            hi = min(len(images), j + 9)
            for i in range(lo, hi):
                d = abs(image_timestamp(images[i]) - t)
                if d < best_dt:
                    best_dt = d
                    best_i = i
            j = best_i
            dt = best_dt
        if dt > max_dt:
            continue
        frames.append((pose, images[j], dt))
    return frames


def resolve_sources(scan_dir, fastlivo_log, gs_root):
    scan_dir = pathlib.Path(scan_dir).resolve()
    scan_id = scan_dir.name
    gs_raw = pathlib.Path(gs_root) / scan_id / "raw" if gs_root else None
    log = pathlib.Path(fastlivo_log).resolve() if fastlivo_log else None

    lidar_pose = find_first_existing(
        [
            scan_dir / "lidar_poses.txt",
            scan_dir / "replay_src" / "lidar_poses.txt",
            gs_raw / "lidar_poses.txt" if gs_raw else None,
            log / "pcd" / "lidar_poses.txt" if log else None,
        ]
    )
    image_pose = find_first_existing(
        [
            scan_dir / "image_poses.txt",
            scan_dir / "replay_src" / "image_poses.txt",
            gs_raw / "image_poses.txt" if gs_raw else None,
            log / "image" / "image_poses.txt" if log else None,
        ]
    )
    image_dirs = [
        scan_dir / "images",
        scan_dir / "image",
        scan_dir / "replay_src" / "image",
        gs_raw / "image" if gs_raw else None,
        log / "image" if log else None,
    ]
    images = collect_images([d for d in image_dirs if d])
    return {
        "scan_id": scan_id,
        "scan_dir": scan_dir,
        "lidar_pose": lidar_pose,
        "image_pose": image_pose,
        "images": images,
        "gs_raw": gs_raw,
        "log": log,
    }


def stage_sources_into_scan(scan_dir, sources):
    """Copy poses/images into scan_dir so map folder is self-contained."""
    scan_dir = pathlib.Path(scan_dir)
    copied = []
    if sources["lidar_pose"] and not (scan_dir / "lidar_poses.txt").exists():
        shutil.copy2(sources["lidar_pose"], scan_dir / "lidar_poses.txt")
        copied.append(str(scan_dir / "lidar_poses.txt"))
    if sources["image_pose"] and not (scan_dir / "image_poses.txt").exists():
        shutil.copy2(sources["image_pose"], scan_dir / "image_poses.txt")
        copied.append(str(scan_dir / "image_poses.txt"))
    # Keep a local image cache only if scan_dir has none.
    local_images = collect_images([scan_dir / "images", scan_dir / "image"])
    if not local_images and sources["images"]:
        dst_dir = scan_dir / "images"
        dst_dir.mkdir(parents=True, exist_ok=True)
        for src in sources["images"]:
            dst = dst_dir / src.name
            if not dst.exists():
                shutil.copy2(src, dst)
                copied.append(str(dst))
    return copied


def build_pack(
    scan_dir,
    fastlivo_log=None,
    gs_root=None,
    max_images=1500,
    max_path=4000,
    jpeg_quality=80,
    image_width=640,
    max_dt=0.08,
):
    scan_dir = pathlib.Path(scan_dir).resolve()
    if not scan_dir.exists():
        return {"ok": False, "error": f"scan dir missing: {scan_dir}"}

    home = pathlib.Path.home()
    if gs_root is None:
        gs_root = home / "fast_livo2_data" / "output" / "gs_livo_datasets"
    if fastlivo_log is None:
        fastlivo_log = home / "fast_livo2_ws" / "src" / "FAST-LIVO2" / "Log"

    sources = resolve_sources(scan_dir, fastlivo_log, gs_root)
    staged = stage_sources_into_scan(scan_dir, sources)
    # Re-resolve after staging so local files win.
    sources = resolve_sources(scan_dir, fastlivo_log, gs_root)

    lidar_poses = parse_pose_file(sources["lidar_pose"]) if sources["lidar_pose"] else []
    image_poses = parse_pose_file(sources["image_pose"]) if sources["image_pose"] else []
    images = sources["images"]

    path_poses = lidar_poses or image_poses
    if not path_poses:
        return {
            "ok": False,
            "error": "no poses found (lidar_poses.txt / image_poses.txt)",
            "scan_id": sources["scan_id"],
            "scan_dir": str(scan_dir),
        }

    path_poses = downsample_list(path_poses, max_path)
    path = [pose_entry(p) for p in path_poses]

    matched = match_frames(image_poses or path_poses, images, max_dt=max_dt)
    matched = downsample_list(matched, max_images)

    replay_dir = scan_dir / "replay"
    images_out = replay_dir / "images"
    if replay_dir.exists():
        shutil.rmtree(replay_dir, ignore_errors=True)
    images_out.mkdir(parents=True, exist_ok=True)

    frames = []
    image_errors = 0
    for idx, (pose, src_img, dt) in enumerate(matched):
        out_name = f"{idx:06d}.jpg"
        try:
            written = convert_or_copy_image(
                src_img,
                images_out / out_name,
                quality=jpeg_quality,
                width=image_width,
            )
            rel = f"images/{written.name}"
            entry = pose_entry(pose)
            entry["image"] = rel
            entry["dt"] = round(float(dt), 4)
            frames.append(entry)
        except Exception:
            image_errors += 1

    t0 = path[0]["t"]
    t1 = path[-1]["t"]
    trajectory = {
        "scan_id": sources["scan_id"],
        "coord": "fast_livo_camera_init",
        "t0": t0,
        "t1": t1,
        "duration": round(max(0.0, t1 - t0), 6),
        "path": path,
        "frames": frames,
        "source": {
            "path_poses": str(sources["lidar_pose"]) if sources["lidar_pose"] else None,
            "frame_poses": str(sources["image_pose"]) if sources["image_pose"] else None,
            "images_from": "local_or_gs_or_log",
            "image_count_src": len(images),
            "path_count": len(path),
            "frame_count": len(frames),
        },
    }
    traj_path = replay_dir / "trajectory.json"
    traj_path.write_text(json.dumps(trajectory, ensure_ascii=False), encoding="utf-8")

    manifest = {
        "ok": True,
        "scan_id": sources["scan_id"],
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "trajectory": "trajectory.json",
        "path_count": len(path),
        "frame_count": len(frames),
        "image_errors": image_errors,
        "staged": staged,
        "duration": trajectory["duration"],
        "has_images": bool(frames),
    }
    (replay_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # Patch metadata.json if present.
    meta_path = scan_dir / "metadata.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    meta["replay"] = {
        "ok": True,
        "path": "replay/trajectory.json",
        "manifest": "replay/manifest.json",
        "path_count": len(path),
        "frame_count": len(frames),
        "duration": trajectory["duration"],
        "built_at": manifest["created_at"],
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "scan_id": sources["scan_id"],
        "scan_dir": str(scan_dir),
        "trajectory": str(traj_path),
        "path_count": len(path),
        "frame_count": len(frames),
        "duration": trajectory["duration"],
        "image_errors": image_errors,
        "staged": staged,
    }


def main():
    parser = argparse.ArgumentParser(description="Build map viewer replay pack")
    parser.add_argument("--scan-dir", default="", help="Single scan directory")
    parser.add_argument("--all-under", default="", help="Build for every subdir under this maps root")
    parser.add_argument("--fastlivo-log", default="")
    parser.add_argument("--gs-root", default="")
    parser.add_argument("--max-images", type=int, default=1500)
    parser.add_argument("--max-path", type=int, default=4000)
    parser.add_argument("--jpeg-quality", type=int, default=80)
    parser.add_argument("--image-width", type=int, default=640)
    parser.add_argument("--max-dt", type=float, default=0.08)
    args = parser.parse_args()

    kwargs = {
        "fastlivo_log": args.fastlivo_log or None,
        "gs_root": args.gs_root or None,
        "max_images": args.max_images,
        "max_path": args.max_path,
        "jpeg_quality": args.jpeg_quality,
        "image_width": args.image_width,
        "max_dt": args.max_dt,
    }

    results = []
    if args.all_under:
        root = pathlib.Path(args.all_under)
        if not root.exists():
            print(json.dumps({"ok": False, "error": f"missing root: {root}"}, ensure_ascii=False))
            return 1
        for child in sorted(p for p in root.iterdir() if p.is_dir()):
            results.append(build_pack(child, **kwargs))
    elif args.scan_dir:
        results.append(build_pack(args.scan_dir, **kwargs))
    else:
        print(json.dumps({"ok": False, "error": "provide --scan-dir or --all-under"}, ensure_ascii=False))
        return 1

    ok = any(r.get("ok") for r in results)
    out = results[0] if len(results) == 1 else {"ok": ok, "results": results}
    print(json.dumps(out, ensure_ascii=False))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
