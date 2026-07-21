#!/usr/bin/env python3
import argparse
import json
import pathlib
import shutil
import sys
import tempfile
import time

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from jr_usb_camera_calibration import parse_corners, solve_from_images


def latest_calib_dir(root):
    root = pathlib.Path(root).expanduser()
    candidates = [p for p in root.iterdir() if p.is_dir()]
    if not candidates:
        raise RuntimeError(f"No calibration output directories found under {root}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def summarize(result, output_dir):
    print(f"ok: {result.get('ok')}")
    print(f"accepted: {result.get('accepted')}")
    print(f"rejected: {result.get('rejected')}")
    if result.get("ok"):
        print(f"rms_px: {result.get('rms_px'):.6f}")
        print(f"output: {output_dir}")
        worst = sorted(result.get("per_image_errors", []), key=lambda x: x.get("error_px", 0.0), reverse=True)[:8]
        if worst:
            print("worst_images:")
            for item in worst:
                print(f"  {item['error_px']:.6f} px  {item['image']}")
    else:
        print(result.get("message", "solve failed"))


def main():
    default_root = pathlib.Path.home() / "fast_livo2_data" / "calib" / "camera_intrinsics" / "jr_mvs"
    parser = argparse.ArgumentParser(description="Offline JR OpenCV camera calibration solver.")
    parser.add_argument("--calib-dir", default=None, help="Calibration directory containing images/. Defaults to latest.")
    parser.add_argument("--root", default=str(default_root), help="Root used when --calib-dir is omitted.")
    parser.add_argument("--images-dir", default=None, help="Override images directory.")
    parser.add_argument("--output-dir", default=None, help="Output directory. Defaults to calib dir.")
    parser.add_argument("--inner-corners", default="11x8")
    parser.add_argument("--square-size", type=float, default=0.025)
    parser.add_argument("--min-count", type=int, default=25)
    parser.add_argument("--reject-error-px", type=float, default=0.0, help="Optional one-pass outlier rejection threshold.")
    args = parser.parse_args()

    calib_dir = pathlib.Path(args.calib_dir).expanduser() if args.calib_dir else latest_calib_dir(args.root)
    images_dir = pathlib.Path(args.images_dir).expanduser() if args.images_dir else calib_dir / "images"
    output_dir = pathlib.Path(args.output_dir).expanduser() if args.output_dir else calib_dir
    pattern_size = parse_corners(args.inner_corners)

    print(f"calib_dir: {calib_dir}")
    print(f"images_dir: {images_dir}")
    print(f"output_dir: {output_dir}")
    result = solve_from_images(images_dir, output_dir, pattern_size, args.square_size, args.min_count)
    summarize(result, output_dir)

    if not result.get("ok") or args.reject_error_px <= 0:
        return 0 if result.get("ok") else 2

    keep = [pathlib.Path(item["image"]) for item in result.get("per_image_errors", []) if item.get("error_px", 0.0) <= args.reject_error_px]
    dropped = [item for item in result.get("per_image_errors", []) if item.get("error_px", 0.0) > args.reject_error_px]
    if len(keep) < args.min_count:
        print(f"skip filtered solve: only {len(keep)} images under threshold {args.reject_error_px}")
        return 0

    filtered_output = output_dir / f"filtered_{time.strftime('%Y%m%d-%H%M%S')}"
    filtered_output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="jr_calib_filtered_") as tmp:
        tmp_images = pathlib.Path(tmp)
        for path in keep:
            shutil.copy2(path, tmp_images / path.name)
        filtered = solve_from_images(tmp_images, filtered_output, pattern_size, args.square_size, args.min_count)
    (filtered_output / "dropped_images.json").write_text(json.dumps(dropped, indent=2), encoding="utf-8")
    print("")
    print(f"filtered threshold: {args.reject_error_px}")
    print(f"dropped: {len(dropped)}")
    summarize(filtered, filtered_output)
    return 0 if filtered.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
