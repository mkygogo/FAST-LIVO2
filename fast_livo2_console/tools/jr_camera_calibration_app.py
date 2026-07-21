#!/usr/bin/env python3
import argparse
from collections import deque
import json
import math
import pathlib
import time

import cv2
import numpy as np
import rospy
from cv_bridge import CvBridge
from sensor_msgs.msg import Image


SUPPORTED_EXTS = (".jpg", ".jpeg", ".png", ".bmp")


def parse_corners(value):
    cols, rows = value.lower().replace(" ", "").split("x", 1)
    return int(cols), int(rows)


def image_sharpness(gray):
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def find_corners(gray, pattern_size):
    flags = cv2.CALIB_CB_NORMALIZE_IMAGE
    if hasattr(cv2, "findChessboardCornersSB"):
        ok, corners = cv2.findChessboardCornersSB(gray, pattern_size, flags)
        if ok:
            return True, corners.astype(np.float32)

    classic_flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
    ok, corners = cv2.findChessboardCorners(gray, pattern_size, classic_flags)
    if ok:
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 60, 0.0005)
        corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)
    return ok, corners


def yaml_list(values, precision=12):
    return "[" + ", ".join(f"{float(v):.{precision}g}" for v in values) + "]"


def write_ros_yaml(path, width, height, camera_matrix, dist_coeffs):
    projection = np.zeros((3, 4), dtype=np.float64)
    projection[:3, :3] = camera_matrix
    text = "\n".join([
        f"image_width: {width}",
        f"image_height: {height}",
        "camera_name: jr_hikrobot_camera",
        "camera_matrix:",
        "  rows: 3",
        "  cols: 3",
        f"  data: {yaml_list(camera_matrix.reshape(-1))}",
        "distortion_model: plumb_bob",
        "distortion_coefficients:",
        "  rows: 1",
        "  cols: 5",
        f"  data: {yaml_list(dist_coeffs[:5])}",
        "rectification_matrix:",
        "  rows: 3",
        "  cols: 3",
        f"  data: {yaml_list(np.eye(3).reshape(-1))}",
        "projection_matrix:",
        "  rows: 3",
        "  cols: 4",
        f"  data: {yaml_list(projection.reshape(-1))}",
        "",
    ])
    path.write_text(text, encoding="utf-8")


def write_fast_calib2_yaml(path, camera_matrix, dist_coeffs):
    fx = camera_matrix[0, 0]
    fy = camera_matrix[1, 1]
    cx = camera_matrix[0, 2]
    cy = camera_matrix[1, 2]
    coeffs = list(dist_coeffs[:5]) + [0.0] * max(0, 5 - len(dist_coeffs))
    text = "\n".join([
        "# Paste these values into FAST-Calib2 config/qr_params.yaml",
        f"fx: {fx:.12f}",
        f"fy: {fy:.12f}",
        f"cx: {cx:.12f}",
        f"cy: {cy:.12f}",
        f"k1: {coeffs[0]:.15f}",
        f"k2: {coeffs[1]:.15f}",
        f"p1: {coeffs[2]:.15f}",
        f"p2: {coeffs[3]:.15f}",
        f"k3: {coeffs[4]:.15f}",
        "",
    ])
    path.write_text(text, encoding="utf-8")


def object_points(pattern_size, square_size):
    objp = np.zeros((pattern_size[0] * pattern_size[1], 3), np.float32)
    objp[:, :2] = np.mgrid[0:pattern_size[0], 0:pattern_size[1]].T.reshape(-1, 2)
    objp *= float(square_size)
    return objp


def reprojection_errors(objpoints, imgpoints, rvecs, tvecs, camera_matrix, dist_coeffs):
    errors = []
    for i, obj in enumerate(objpoints):
        projected, _ = cv2.projectPoints(obj, rvecs[i], tvecs[i], camera_matrix, dist_coeffs)
        error = cv2.norm(imgpoints[i], projected, cv2.NORM_L2) / len(projected)
        errors.append(float(error))
    return errors


def robust_keep_mask(errors, z_limit, abs_limit):
    if len(errors) < 4:
        return [True] * len(errors)
    arr = np.asarray(errors, dtype=np.float64)
    median = float(np.median(arr))
    mad = float(np.median(np.abs(arr - median)))
    if mad < 1e-9:
        robust_z = np.zeros_like(arr)
    else:
        robust_z = 0.6745 * (arr - median) / mad
    return [bool(abs(z) <= z_limit and err <= abs_limit) for z, err in zip(robust_z, arr)]


def solve_from_images(images_dir, output_dir, pattern_size, square_size, min_count,
                      outlier_z, outlier_abs):
    image_paths = []
    for ext in SUPPORTED_EXTS:
        image_paths.extend(images_dir.glob(f"*{ext}"))
    image_paths = sorted(image_paths)

    obj_template = object_points(pattern_size, square_size)
    objpoints = []
    imgpoints = []
    accepted = []
    rejected = []
    image_size = None

    for path in image_paths:
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            rejected.append({"image": str(path), "reason": "read failed"})
            continue
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        size = (gray.shape[1], gray.shape[0])
        if image_size is None:
            image_size = size
        elif image_size != size:
            rejected.append({"image": str(path), "reason": "image size mismatch"})
            continue
        ok, corners = find_corners(gray, pattern_size)
        if not ok:
            rejected.append({"image": str(path), "reason": "checkerboard not found"})
            continue
        objpoints.append(obj_template.copy())
        imgpoints.append(corners)
        accepted.append(str(path))

    output_dir.mkdir(parents=True, exist_ok=True)
    if len(accepted) < min_count:
        result = {
            "ok": False,
            "accepted": len(accepted),
            "rejected": len(rejected),
            "message": f"Need at least {min_count} valid images, got {len(accepted)}.",
        }
        (output_dir / "calibration_result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result

    rms, camera_matrix, dist_coeffs, rvecs, tvecs = cv2.calibrateCamera(
        objpoints, imgpoints, image_size, None, None, flags=0
    )
    dist_coeffs = dist_coeffs.reshape(-1)
    initial_errors = reprojection_errors(objpoints, imgpoints, rvecs, tvecs, camera_matrix, dist_coeffs)
    keep = robust_keep_mask(initial_errors, outlier_z, outlier_abs)

    removed = []
    if keep.count(True) >= min_count and keep.count(False) > 0:
        removed = [
            {"image": accepted[i], "error_px": initial_errors[i], "reason": "outlier"}
            for i, kept in enumerate(keep) if not kept
        ]
        objpoints = [obj for obj, kept in zip(objpoints, keep) if kept]
        imgpoints = [pts for pts, kept in zip(imgpoints, keep) if kept]
        accepted = [path for path, kept in zip(accepted, keep) if kept]
        rms, camera_matrix, dist_coeffs, rvecs, tvecs = cv2.calibrateCamera(
            objpoints, imgpoints, image_size, None, None, flags=0
        )
        dist_coeffs = dist_coeffs.reshape(-1)

    final_errors = reprojection_errors(objpoints, imgpoints, rvecs, tvecs, camera_matrix, dist_coeffs)
    per_image_errors = [
        {"image": accepted[i], "error_px": float(final_errors[i])}
        for i in range(len(accepted))
    ]

    write_ros_yaml(output_dir / "camera_intrinsics.yaml", image_size[0], image_size[1], camera_matrix, dist_coeffs)
    write_fast_calib2_yaml(output_dir / "fast_calib2_intrinsics.yaml", camera_matrix, dist_coeffs)

    result = {
        "ok": True,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "inner_corners": f"{pattern_size[0]}x{pattern_size[1]}",
        "square_size_m": float(square_size),
        "image_size": list(image_size),
        "accepted": len(accepted),
        "rejected": len(rejected),
        "removed_outliers": removed,
        "rms_px": float(rms),
        "mean_reprojection_error_px": float(np.mean(final_errors)),
        "max_reprojection_error_px": float(np.max(final_errors)),
        "camera_matrix": camera_matrix.tolist(),
        "distortion_coefficients": dist_coeffs.tolist(),
        "per_image_errors": per_image_errors,
        "outputs": {
            "ros_yaml": str(output_dir / "camera_intrinsics.yaml"),
            "fast_calib2_yaml": str(output_dir / "fast_calib2_intrinsics.yaml"),
            "json": str(output_dir / "calibration_result.json"),
            "report": str(output_dir / "report.txt"),
        },
    }
    (output_dir / "calibration_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report_lines = [
        "JR Scanner OpenCV camera intrinsic calibration",
        "=" * 60,
        f"created_at: {result['created_at']}",
        f"inner_corners: {result['inner_corners']}",
        f"square_size_m: {square_size}",
        f"image_size: {image_size[0]}x{image_size[1]}",
        f"accepted_images: {len(accepted)}",
        f"rejected_images: {len(rejected)}",
        f"removed_outliers: {len(removed)}",
        f"rms_px: {rms:.6f}",
        f"mean_reprojection_error_px: {result['mean_reprojection_error_px']:.6f}",
        f"max_reprojection_error_px: {result['max_reprojection_error_px']:.6f}",
        "",
        "camera_matrix:",
        np.array2string(camera_matrix, precision=12, suppress_small=False),
        "",
        "distortion_coefficients:",
        np.array2string(dist_coeffs, precision=12, suppress_small=False),
        "",
        "outputs:",
        f"camera_intrinsics.yaml: {output_dir / 'camera_intrinsics.yaml'}",
        f"fast_calib2_intrinsics.yaml: {output_dir / 'fast_calib2_intrinsics.yaml'}",
        f"calibration_result.json: {output_dir / 'calibration_result.json'}",
        "",
        "per_image_errors:",
    ]
    for item in per_image_errors:
        report_lines.append(f"{item['error_px']:.6f} px  {item['image']}")
    if removed:
        report_lines.extend(["", "removed_outliers:"])
        for item in removed:
            report_lines.append(f"{item['error_px']:.6f} px  {item['image']}")
    (output_dir / "report.txt").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    return result


class JRCalibrationApp:
    def __init__(self, args):
        self.args = args
        self.pattern_size = parse_corners(args.inner_corners)
        self.output_dir = pathlib.Path(args.output_dir).expanduser()
        self.images_dir = self.output_dir / "images"
        self.overlays_dir = self.output_dir / "overlays"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.overlays_dir.mkdir(parents=True, exist_ok=True)

        self.bridge = CvBridge()
        self.latest_image = None
        self.latest_stamp = 0.0
        self.samples = []
        self.recent_sharpness = deque(maxlen=args.sharpness_window)
        self.last_capture_time = 0.0
        self.ok_since = None
        self.auto_capture = args.auto_capture
        self.solved = False
        self.solving = False
        self.last_solve_count = 0
        self.quit_requested = False
        self.status_message = "Waiting for camera image..."
        self.buttons = []
        self.last_detection_time = 0.0
        self.last_processed_stamp = 0.0
        self.last_found = False
        self.last_corners = None
        self.last_quality = None

        rospy.init_node("jr_camera_calibration_app", anonymous=True)
        rospy.Subscriber(args.image_topic, Image, self.on_image, queue_size=1, buff_size=2**24)

    def on_image(self, msg):
        try:
            image = self.bridge.imgmsg_to_cv2(msg, desired_encoding="bgr8")
        except Exception as exc:
            rospy.logwarn("Failed to convert image: %s", exc)
            return
        self.latest_image = image
        self.latest_stamp = msg.header.stamp.to_sec() if msg.header.stamp else time.time()

    def corner_features(self, corners, width, height):
        pts = corners.reshape(-1, 2)
        min_xy = pts.min(axis=0)
        max_xy = pts.max(axis=0)
        center = pts.mean(axis=0)
        bbox_w, bbox_h = max_xy - min_xy
        area_ratio = float((bbox_w * bbox_h) / max(1, width * height))
        margin = float(min(min_xy[0], min_xy[1], width - max_xy[0], height - max_xy[1]))

        cols, rows = self.pattern_size
        grid = pts.reshape(rows, cols, 2)
        top = np.linalg.norm(grid[0, -1] - grid[0, 0])
        bottom = np.linalg.norm(grid[-1, -1] - grid[-1, 0])
        left = np.linalg.norm(grid[-1, 0] - grid[0, 0])
        right = np.linalg.norm(grid[-1, -1] - grid[0, -1])
        tilt = max(abs(math.log((top + 1e-6) / (bottom + 1e-6))),
                   abs(math.log((left + 1e-6) / (right + 1e-6))))
        angle = math.degrees(math.atan2(grid[0, -1][1] - grid[0, 0][1],
                                        grid[0, -1][0] - grid[0, 0][0]))
        return {
            "center_x": float(center[0] / width),
            "center_y": float(center[1] / height),
            "area_ratio": area_ratio,
            "margin_px": margin,
            "tilt": float(tilt),
            "angle_deg": float(angle),
        }

    def bucket(self, features):
        area = features.get("area_ratio", 0.0)
        if area < self.args.far_area_ratio:
            return "far"
        if area > self.args.near_area_ratio:
            return "near"
        return "mid"

    def is_duplicate(self, features):
        for sample in self.samples[-24:]:
            other = sample.get("features", {})
            d_center = math.hypot(features["center_x"] - other.get("center_x", -9),
                                  features["center_y"] - other.get("center_y", -9))
            d_scale = abs(features["area_ratio"] - other.get("area_ratio", -9))
            d_tilt = abs(features["tilt"] - other.get("tilt", -9))
            d_angle = abs((features["angle_deg"] - other.get("angle_deg", 999) + 90) % 180 - 90)
            if (d_center < self.args.min_center_dist and
                    d_scale < self.args.min_area_delta and
                    d_tilt < 0.12 and
                    d_angle < 18):
                return True
        return False

    def coverage(self):
        cov = {
            "center": False,
            "top_left": False,
            "top_right": False,
            "bottom_left": False,
            "bottom_right": False,
            "near": False,
            "mid": False,
            "far": False,
            "tilted": False,
        }
        for sample in self.samples:
            f = sample.get("features", {})
            x = f.get("center_x", 0.5)
            y = f.get("center_y", 0.5)
            area = f.get("area_ratio", 0.0)
            tilt = f.get("tilt", 0.0)
            if 0.35 <= x <= 0.65 and 0.35 <= y <= 0.65:
                cov["center"] = True
            if x < 0.45 and y < 0.45:
                cov["top_left"] = True
            if x > 0.55 and y < 0.45:
                cov["top_right"] = True
            if x < 0.45 and y > 0.55:
                cov["bottom_left"] = True
            if x > 0.55 and y > 0.55:
                cov["bottom_right"] = True
            if area > self.args.near_area_ratio:
                cov["near"] = True
            elif area >= self.args.far_area_ratio:
                cov["mid"] = True
            else:
                cov["far"] = True
            if tilt > 0.16:
                cov["tilted"] = True
        return cov

    def bucket_counts(self):
        counts = {"near": 0, "mid": 0, "far": 0}
        for sample in self.samples:
            bucket = sample.get("bucket")
            if bucket in counts:
                counts[bucket] += 1
        return counts

    def next_bucket(self):
        counts = self.bucket_counts()
        targets = {
            "near": self.args.target_near,
            "mid": self.args.target_mid,
            "far": self.args.target_far,
        }
        missing = sorted(targets, key=lambda key: counts[key] / max(1, targets[key]))
        for key in missing:
            if counts[key] < targets[key]:
                return key
        return "any"

    def sharpness_threshold(self, features):
        threshold = self.args.min_sharpness
        if self.recent_sharpness:
            adaptive = float(np.median(self.recent_sharpness)) * self.args.adaptive_scale
            threshold = max(self.args.min_sharpness_floor, min(threshold, adaptive))
        if features and self.bucket(features) == "far":
            threshold *= self.args.far_sharpness_relax
        return threshold

    def quality(self, image, gray, corners):
        height, width = gray.shape[:2]
        found = corners is not None
        blur = image_sharpness(gray)
        mean = float(gray.mean())
        dark_ratio = float(np.mean(gray < 18))
        bright_ratio = float(np.mean(gray > 245))

        reasons = []
        features = None
        bucket = None
        if not found:
            reasons.append("checkerboard not found")
        else:
            features = self.corner_features(corners, width, height)
            bucket = self.bucket(features)
            self.recent_sharpness.append(blur)
            if features["margin_px"] < max(18, min(width, height) * 0.035):
                reasons.append("board too close to edge")
            if features["area_ratio"] < 0.045:
                reasons.append("board too far")
            if features["area_ratio"] > 0.72:
                reasons.append("board too near")
            if self.is_duplicate(features):
                reasons.append("pose too similar")

        threshold = self.sharpness_threshold(features)
        if blur < threshold:
            reasons.append("image too blurry")
        if mean < 35 or dark_ratio > 0.45:
            reasons.append("image too dark")
        if mean > 220 or bright_ratio > 0.18:
            reasons.append("image overexposed")

        return {
            "ok": not reasons,
            "reason": "OK auto capture ready" if not reasons else " / ".join(reasons[:2]),
            "features": features,
            "bucket": bucket,
            "metrics": {
                "blur": round(blur, 1),
                "sharpness_threshold": round(threshold, 1),
                "mean": round(mean, 1),
                "dark_ratio": round(dark_ratio, 3),
                "bright_ratio": round(bright_ratio, 3),
            },
        }

    def should_capture(self, quality):
        if self.solved or self.solving or not self.auto_capture or not quality["ok"]:
            self.ok_since = None
            return False
        now = time.time()
        if self.ok_since is None:
            self.ok_since = now
        return (
            now - self.ok_since >= self.args.stable_seconds and
            now - self.last_capture_time >= self.args.cooldown_seconds
        )

    def capture(self, image, overlay, quality, forced=False):
        index = len(self.samples) + 1
        name = f"calib_{index:03d}.jpg"
        overlay_name = f"calib_{index:03d}_overlay.jpg"
        image_path = self.images_dir / name
        overlay_path = self.overlays_dir / overlay_name
        cv2.imwrite(str(image_path), image, [int(cv2.IMWRITE_JPEG_QUALITY), 96])
        cv2.imwrite(str(overlay_path), overlay, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        sample = {
            "index": index,
            "image": str(image_path),
            "overlay": str(overlay_path),
            "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "forced": bool(forced),
            "bucket": quality.get("bucket"),
            "features": quality.get("features") or {},
            "metrics": quality.get("metrics") or {},
        }
        self.samples.append(sample)
        (self.output_dir / "manifest.json").write_text(
            json.dumps({
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "image_topic": self.args.image_topic,
                "inner_corners": self.args.inner_corners,
                "square_size_m": self.args.square_size,
                "samples": self.samples,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.last_capture_time = time.time()
        self.ok_since = None
        self.status_message = f"Captured {index}/{self.args.target_count}"
        print(self.status_message, flush=True)

    def enough_to_auto_solve(self):
        count = len(self.samples)
        if count < self.args.target_count:
            return False
        cov = self.coverage()
        basic = cov["near"] and cov["mid"] and cov["far"] and (cov["center"] or cov["tilted"])
        if basic:
            return True
        return count >= self.args.max_count

    def solve(self, reason):
        if self.solving or self.solved:
            return
        self.solving = True
        self.last_solve_count = len(self.samples)
        self.status_message = f"Solving calibration ({reason})..."
        print(self.status_message, flush=True)
        try:
            result = solve_from_images(
                self.images_dir,
                self.output_dir,
                self.pattern_size,
                self.args.square_size,
                self.args.min_count,
                self.args.outlier_z,
                self.args.outlier_abs,
            )
            if result.get("ok"):
                self.solved = True
                self.auto_capture = False
                self.status_message = (
                    f"Solved: RMS {result.get('rms_px', 0):.4f}px, "
                    f"{result.get('accepted', 0)} images"
                )
                print(self.status_message, flush=True)
                print(f"Output: {self.output_dir}", flush=True)
            else:
                self.status_message = result.get("message", "Calibration failed")
                print(self.status_message, flush=True)
        except Exception as exc:
            self.status_message = f"Solve failed: {exc}"
            print(self.status_message, flush=True)
        finally:
            self.solving = False

    def reset(self):
        for folder in (self.images_dir, self.overlays_dir):
            for path in folder.glob("*"):
                if path.is_file():
                    path.unlink()
        for name in ("manifest.json", "calibration_result.json", "report.txt",
                     "camera_intrinsics.yaml", "fast_calib2_intrinsics.yaml"):
            path = self.output_dir / name
            if path.exists():
                path.unlink()
        self.samples = []
        self.recent_sharpness.clear()
        self.last_capture_time = 0.0
        self.ok_since = None
        self.solved = False
        self.solving = False
        self.last_solve_count = 0
        self.auto_capture = True
        self.status_message = "Reset complete. Auto capture ON."
        print(self.status_message, flush=True)

    def draw_buttons(self, canvas):
        h, w = canvas.shape[:2]
        labels = [
            ("auto", "Pause Auto" if self.auto_capture else "Resume Auto"),
            ("solve", "Solve Now"),
            ("reset", "Reset"),
            ("quit", "Quit"),
        ]
        margin = 12
        gap = 10
        button_h = 54
        y1 = h - button_h - margin
        button_w = max(120, int((w - margin * 2 - gap * (len(labels) - 1)) / len(labels)))
        self.buttons = []
        for i, (name, label) in enumerate(labels):
            x1 = margin + i * (button_w + gap)
            x2 = x1 + button_w
            y2 = y1 + button_h
            color = (40, 120, 70) if name == "auto" and self.auto_capture else (55, 65, 75)
            if name == "solve":
                color = (35, 95, 150)
            if name == "reset":
                color = (130, 95, 35)
            if name == "quit":
                color = (130, 45, 45)
            cv2.rectangle(canvas, (x1, y1), (x2, y2), color, -1)
            cv2.rectangle(canvas, (x1, y1), (x2, y2), (230, 230, 230), 1)
            cv2.putText(canvas, label, (x1 + 14, y1 + 35), cv2.FONT_HERSHEY_SIMPLEX,
                        0.72, (255, 255, 255), 2, cv2.LINE_AA)
            self.buttons.append((name, x1, y1, x2, y2))

    def draw_status(self, display, quality):
        image_h, image_w = display.shape[:2]
        side_w = 420
        bottom_h = 86
        canvas = np.zeros((image_h + bottom_h, image_w + side_w, 3), dtype=np.uint8)
        canvas[:] = (24, 29, 35)
        canvas[:image_h, :image_w] = display
        side = canvas[:image_h, image_w:]
        side[:] = (248, 250, 252)

        ok = bool(quality and quality.get("ok")) and not self.solved
        status_color = (25, 128, 65) if ok else (35, 35, 180)
        if self.solved:
            status_color = (120, 90, 20)
        cv2.rectangle(side, (18, 18), (side_w - 18, 86), status_color, -1)
        label = "SOLVED" if self.solved else ("OK" if ok else "NO")
        cv2.putText(side, label, (40, 66), cv2.FONT_HERSHEY_SIMPLEX, 1.5,
                    (255, 255, 255), 3, cv2.LINE_AA)

        metrics = quality.get("metrics", {}) if quality else {}
        cov = self.coverage()
        counts = self.bucket_counts()
        lines = [
            f"Images: {len(self.samples)}/{self.args.target_count}",
            f"Auto: {'ON' if self.auto_capture else 'OFF'}",
            f"Next: {self.next_bucket()}",
            f"Blur: {metrics.get('blur', '-')} / {metrics.get('sharpness_threshold', '-')}",
            f"Bucket: {quality.get('bucket') if quality else '-'}",
            f"near/mid/far: {counts['near']}/{counts['mid']}/{counts['far']}",
            f"Coverage: C:{int(cov['center'])} N:{int(cov['near'])} M:{int(cov['mid'])} F:{int(cov['far'])} T:{int(cov['tilted'])}",
            f"Output: {self.output_dir}",
        ]
        y = 126
        for line in lines:
            cv2.putText(side, line, (22, y), cv2.FONT_HERSHEY_SIMPLEX, 0.58,
                        (30, 40, 50), 1, cv2.LINE_AA)
            y += 34

        reason = self.status_message
        if quality:
            reason = quality.get("reason") or reason
        for i, chunk in enumerate([reason[j:j + 36] for j in range(0, len(reason), 36)][:3]):
            cv2.putText(side, chunk, (22, image_h - 92 + i * 26), cv2.FONT_HERSHEY_SIMPLEX,
                        0.58, (20, 30, 40), 1, cv2.LINE_AA)

        cv2.putText(canvas, "Move checkerboard. Auto capture and auto solve are enabled.",
                    (18, image_h + 26), cv2.FONT_HERSHEY_SIMPLEX, 0.62,
                    (230, 238, 245), 1, cv2.LINE_AA)
        self.draw_buttons(canvas)
        return canvas

    def handle_click(self, event, x, y, flags, param):
        if event != cv2.EVENT_LBUTTONUP:
            return
        for name, x1, y1, x2, y2 in self.buttons:
            if x1 <= x <= x2 and y1 <= y <= y2:
                if name == "auto":
                    self.auto_capture = not self.auto_capture
                    self.status_message = f"Auto capture {'ON' if self.auto_capture else 'OFF'}"
                elif name == "solve":
                    if len(self.samples) >= self.args.min_count:
                        self.solve("button")
                    else:
                        self.status_message = f"Need {self.args.min_count} images before solving"
                elif name == "reset":
                    self.reset()
                elif name == "quit":
                    self.quit_requested = True
                break

    def detect_corners_for_frame(self, gray):
        height, width = gray.shape[:2]
        scale = min(1.0, float(self.args.detect_width) / max(1, width))
        detect_gray = gray
        if scale < 1.0:
            detect_gray = cv2.resize(
                gray,
                (max(1, int(width * scale)), max(1, int(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        found, corners = find_corners(detect_gray, self.pattern_size)
        if found and scale < 1.0:
            corners = corners / scale
        return found, corners

    def process_frame(self):
        image = self.latest_image
        if image is None:
            blank = np.zeros((540, 960, 3), dtype=np.uint8)
            cv2.putText(blank, "Waiting for /left_camera/image...", (40, 270),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (230, 230, 230), 2, cv2.LINE_AA)
            return self.draw_status(blank, None)

        now = time.time()
        detect_interval = 1.0 / max(self.args.detect_hz, 0.1)
        detected_this_frame = (
            self.last_quality is None or
            (self.latest_stamp != self.last_processed_stamp and
             now - self.last_detection_time >= detect_interval)
        )
        if detected_this_frame:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            found, corners = self.detect_corners_for_frame(gray)
            quality = self.quality(image, gray, corners if found else None)
            self.last_detection_time = now
            self.last_processed_stamp = self.latest_stamp
            self.last_found = found
            self.last_corners = corners
            self.last_quality = quality
        else:
            found = self.last_found
            corners = self.last_corners
            quality = self.last_quality
        overlay = image.copy()
        if found:
            cv2.drawChessboardCorners(overlay, self.pattern_size, corners, found)

        if detected_this_frame and self.should_capture(quality):
            self.capture(image, overlay, quality)

        if (self.args.auto_solve and not self.solved and
                len(self.samples) >= self.args.min_count and
                len(self.samples) > self.last_solve_count):
            if self.enough_to_auto_solve():
                self.solve("auto")

        h, w = overlay.shape[:2]
        scale = min(1.0, self.args.preview_width / max(1, w), self.args.preview_height / max(1, h))
        if scale < 1.0:
            overlay = cv2.resize(overlay, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        return self.draw_status(overlay, quality)

    def run(self):
        print(f"JR camera calibration app started. Output: {self.output_dir}", flush=True)
        print("Controls: touch buttons, A auto, S force save, C solve, R reset, Q/Esc quit", flush=True)
        cv2.namedWindow(self.args.window_name, cv2.WINDOW_NORMAL)
        cv2.setMouseCallback(self.args.window_name, self.handle_click)
        rate = rospy.Rate(self.args.preview_hz)
        while not rospy.is_shutdown() and not self.quit_requested:
            canvas = self.process_frame()
            cv2.imshow(self.args.window_name, canvas)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), ord("Q"), 27):
                break
            if key in (ord("a"), ord("A")):
                self.auto_capture = not self.auto_capture
                self.status_message = f"Auto capture {'ON' if self.auto_capture else 'OFF'}"
            elif key in (ord("s"), ord("S")) and self.latest_image is not None:
                image = self.latest_image.copy()
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                found, corners = find_corners(gray, self.pattern_size)
                quality = self.quality(image, gray, corners if found else None)
                overlay = image.copy()
                if found:
                    cv2.drawChessboardCorners(overlay, self.pattern_size, corners, found)
                self.capture(image, overlay, quality, forced=True)
            elif key in (ord("c"), ord("C")):
                if len(self.samples) >= self.args.min_count:
                    self.solve("manual")
                else:
                    self.status_message = f"Need {self.args.min_count} images before solving"
            elif key in (ord("r"), ord("R")):
                self.reset()
            rate.sleep()
        cv2.destroyAllWindows()
        print("JR camera calibration app closed.", flush=True)


def main():
    default_output = pathlib.Path.home() / "fast_livo2_data" / "calib" / "camera_intrinsics" / "jr_opencv" / time.strftime("%Y%m%d-%H%M%S")
    parser = argparse.ArgumentParser(description="JR Scanner OpenCV camera calibration app")
    parser.add_argument("--image-topic", default="/left_camera/image")
    parser.add_argument("--output-dir", default=str(default_output))
    parser.add_argument("--inner-corners", default="11x8")
    parser.add_argument("--square-size", type=float, default=0.025)
    parser.add_argument("--target-count", type=int, default=40)
    parser.add_argument("--min-count", type=int, default=25)
    parser.add_argument("--max-count", type=int, default=55)
    parser.add_argument("--target-near", type=int, default=12)
    parser.add_argument("--target-mid", type=int, default=12)
    parser.add_argument("--target-far", type=int, default=10)
    parser.add_argument("--auto-capture", action="store_true", default=True)
    parser.add_argument("--no-auto-capture", action="store_false", dest="auto_capture")
    parser.add_argument("--auto-solve", action="store_true", default=True)
    parser.add_argument("--no-auto-solve", action="store_false", dest="auto_solve")
    parser.add_argument("--preview-hz", type=float, default=5.0)
    parser.add_argument("--preview-width", type=int, default=960)
    parser.add_argument("--preview-height", type=int, default=700)
    parser.add_argument("--detect-hz", type=float, default=2.0)
    parser.add_argument("--detect-width", type=int, default=900)
    parser.add_argument("--stable-seconds", type=float, default=0.7)
    parser.add_argument("--cooldown-seconds", type=float, default=1.3)
    parser.add_argument("--min-sharpness", type=float, default=75.0)
    parser.add_argument("--min-sharpness-floor", type=float, default=45.0)
    parser.add_argument("--sharpness-window", type=int, default=45)
    parser.add_argument("--adaptive-scale", type=float, default=0.82)
    parser.add_argument("--far-sharpness-relax", type=float, default=0.88)
    parser.add_argument("--far-area-ratio", type=float, default=0.08)
    parser.add_argument("--near-area-ratio", type=float, default=0.18)
    parser.add_argument("--min-center-dist", type=float, default=0.10)
    parser.add_argument("--min-area-delta", type=float, default=0.045)
    parser.add_argument("--outlier-z", type=float, default=3.0)
    parser.add_argument("--outlier-abs", type=float, default=0.75)
    parser.add_argument("--window-name", default="JR Camera Calibration")
    args = parser.parse_args()
    JRCalibrationApp(args).run()


if __name__ == "__main__":
    main()
