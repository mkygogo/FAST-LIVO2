#!/usr/bin/env python3
import argparse
from collections import deque
import json
import math
import os
import pathlib
import time

import cv2
import numpy as np


def parse_corners(value):
    cols, rows = value.lower().replace(" ", "").split("x", 1)
    return int(cols), int(rows)


def image_sharpness(gray):
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def focus_score(gray):
    h, w = gray.shape[:2]
    crop_w, crop_h = max(32, int(w * 0.45)), max(32, int(h * 0.45))
    x1, y1 = (w - crop_w) // 2, (h - crop_h) // 2
    roi = gray[y1 : y1 + crop_h, x1 : x1 + crop_w]
    roi = cv2.GaussianBlur(roi, (3, 3), 0)
    gx = cv2.Sobel(roi, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(roi, cv2.CV_32F, 0, 1, ksize=3)
    return float(np.mean(gx * gx + gy * gy))


def clamp(value, lower, upper):
    return max(lower, min(upper, value))


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
    camera_name = os.environ.get("JR_CAMERA_NAME", "jr_usb_camera")
    projection = np.zeros((3, 4), dtype=np.float64)
    projection[:3, :3] = camera_matrix
    text = "\n".join([
        f"image_width: {width}",
        f"image_height: {height}",
        f"camera_name: {camera_name}",
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
    fx, fy = camera_matrix[0, 0], camera_matrix[1, 1]
    cx, cy = camera_matrix[0, 2], camera_matrix[1, 2]
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


def solve_from_images(images_dir, output_dir, pattern_size, square_size, min_count):
    paths = sorted(list(images_dir.glob("*.jpg")) + list(images_dir.glob("*.png")))
    obj_template = object_points(pattern_size, square_size)
    objpoints, imgpoints, accepted, rejected = [], [], [], []
    image_size = None
    for path in paths:
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
        result = {"ok": False, "accepted": len(accepted), "message": f"Need {min_count}, got {len(accepted)}"}
        (output_dir / "calibration_result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result

    rms, camera_matrix, dist_coeffs, rvecs, tvecs = cv2.calibrateCamera(
        objpoints, imgpoints, image_size, None, None, flags=0
    )
    dist_coeffs = dist_coeffs.reshape(-1)
    errors = []
    for i, obj in enumerate(objpoints):
        projected, _ = cv2.projectPoints(obj, rvecs[i], tvecs[i], camera_matrix, dist_coeffs)
        err = cv2.norm(imgpoints[i], projected, cv2.NORM_L2) / len(projected)
        errors.append({"image": accepted[i], "error_px": float(err)})

    write_ros_yaml(output_dir / "camera_intrinsics.yaml", image_size[0], image_size[1], camera_matrix, dist_coeffs)
    write_fast_calib2_yaml(output_dir / "fast_calib2_intrinsics.yaml", camera_matrix, dist_coeffs)
    result = {
        "ok": True,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "image_size": list(image_size),
        "accepted": len(accepted),
        "rejected": len(rejected),
        "rms_px": float(rms),
        "camera_matrix": camera_matrix.tolist(),
        "distortion_coefficients": dist_coeffs.tolist(),
        "per_image_errors": errors,
        "outputs": {
            "ros_yaml": str(output_dir / "camera_intrinsics.yaml"),
            "fast_calib2_yaml": str(output_dir / "fast_calib2_intrinsics.yaml"),
            "json": str(output_dir / "calibration_result.json"),
            "report": str(output_dir / "report.txt"),
        },
    }
    (output_dir / "calibration_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        os.environ.get("JR_CALIB_REPORT_TITLE", "JR USB OpenCV camera calibration"),
        f"created_at: {result['created_at']}",
        f"image_size: {image_size[0]}x{image_size[1]}",
        f"accepted_images: {len(accepted)}",
        f"rejected_images: {len(rejected)}",
        f"rms_px: {rms:.6f}",
        "",
        "camera_matrix:",
        np.array2string(camera_matrix, precision=12),
        "",
        "distortion_coefficients:",
        np.array2string(dist_coeffs, precision=12),
        "",
        "per_image_errors:",
    ]
    for item in errors:
        lines.append(f"{item['error_px']:.6f} px  {item['image']}")
    (output_dir / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


class UsbCalibrationApp:
    def __init__(self, args):
        self.args = args
        self.pattern_size = parse_corners(args.inner_corners)
        self.output_dir = pathlib.Path(args.output_dir).expanduser()
        self.images_dir = self.output_dir / "images"
        self.overlays_dir = self.output_dir / "overlays"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.overlays_dir.mkdir(parents=True, exist_ok=True)
        self.samples = self.load_samples()
        self.next_index = max([int(s.get("index", 0)) for s in self.samples] or [0]) + 1
        self.recent_sharpness = deque(maxlen=45)
        self.last_capture_time = 0.0
        self.last_detection_time = 0.0
        self.last_quality = None
        self.last_found = False
        self.last_corners = None
        self.ok_since = None
        self.auto_capture = True
        self.focus_view = bool(getattr(args, "focus_view", False))
        self.tune_mode = False
        self.tune_index = 0
        self.tune_params = [
            ("exposure_us", "Exp us", 500.0, 100000.0, 5000.0),
            ("gain", "Gain", 0.0, 24.0, 1.0),
            ("gamma", "Gamma", 0.5, 3.0, 0.1),
            ("saturation", "Sat", 0.0, 255.0, 10.0),
            ("sharpness", "Sharp", 0.0, 100.0, 5.0),
        ]
        self.tune_values = {
            "exposure_us": float(getattr(args, "exposure_us", 40000.0) or 40000.0),
            "gain": float(getattr(args, "gain", 0.0) or 0.0),
            "gamma": float(getattr(args, "gamma", 1.0) or 1.0),
            "saturation": float(getattr(args, "saturation", 128.0)),
            "sharpness": float(getattr(args, "sharpness", 0.0)),
        }
        self.camera = None
        self.solved = False
        self.quit_requested = False
        self.status_message = "Starting camera..."
        self.buttons = []
        self.current_frame = None
        self.current_overlay = None
        self.current_quality = None

    def load_samples(self):
        manifest = self.output_dir / "manifest.json"
        if not manifest.exists():
            return []
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            samples = data.get("samples", [])
            print(f"Loaded {len(samples)} existing samples from {manifest}", flush=True)
            return samples
        except Exception as exc:
            print(f"Warning: could not load manifest {manifest}: {exc}", flush=True)
            return []

    def open_camera(self):
        cap = cv2.VideoCapture(self.args.camera_index)
        if not cap.isOpened():
            raise RuntimeError(f"Could not open camera index {self.args.camera_index}")
        if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.args.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.args.height)
        if self.args.fps > 0:
            cap.set(cv2.CAP_PROP_FPS, self.args.fps)
        return cap

    def features(self, corners, width, height):
        pts = corners.reshape(-1, 2)
        min_xy, max_xy = pts.min(axis=0), pts.max(axis=0)
        center = pts.mean(axis=0)
        bbox_w, bbox_h = max_xy - min_xy
        cols, rows = self.pattern_size
        grid = pts.reshape(rows, cols, 2)
        top = np.linalg.norm(grid[0, -1] - grid[0, 0])
        bottom = np.linalg.norm(grid[-1, -1] - grid[-1, 0])
        left = np.linalg.norm(grid[-1, 0] - grid[0, 0])
        right = np.linalg.norm(grid[-1, -1] - grid[0, -1])
        tilt = max(abs(math.log((top + 1e-6) / (bottom + 1e-6))), abs(math.log((left + 1e-6) / (right + 1e-6))))
        return {
            "center_x": float(center[0] / width),
            "center_y": float(center[1] / height),
            "area_ratio": float((bbox_w * bbox_h) / max(1, width * height)),
            "margin_px": float(min(min_xy[0], min_xy[1], width - max_xy[0], height - max_xy[1])),
            "tilt": float(tilt),
        }

    def is_duplicate(self, features):
        center_threshold = getattr(self.args, "duplicate_center", 0.06)
        scale_threshold = getattr(self.args, "duplicate_scale", 0.025)
        tilt_threshold = getattr(self.args, "duplicate_tilt", 0.08)
        for sample in self.samples[-20:]:
            other = sample.get("features", {})
            d_center = math.hypot(features["center_x"] - other.get("center_x", -9), features["center_y"] - other.get("center_y", -9))
            d_scale = abs(features["area_ratio"] - other.get("area_ratio", -9))
            d_tilt = abs(features["tilt"] - other.get("tilt", -9))
            if d_center < center_threshold and d_scale < scale_threshold and d_tilt < tilt_threshold:
                return True
        return False

    def quality(self, image, gray, corners):
        h, w = gray.shape[:2]
        blur = image_sharpness(gray)
        mean = float(gray.mean())
        reasons = []
        features = None
        if corners is None:
            reasons.append("checkerboard not found")
        else:
            features = self.features(corners, w, h)
            self.recent_sharpness.append(blur)
            if features["margin_px"] < max(18, min(w, h) * 0.035):
                reasons.append("board too close to edge")
            if features["area_ratio"] < 0.045:
                reasons.append("board too far")
            if features["area_ratio"] > 0.72:
                reasons.append("board too near")
            if self.is_duplicate(features):
                reasons.append("pose too similar")
        threshold = self.args.min_sharpness
        if self.recent_sharpness:
            threshold = max(self.args.min_sharpness_floor, min(threshold, float(np.median(self.recent_sharpness)) * 0.82))
        if blur < threshold:
            reasons.append("image too blurry")
        if mean < 35:
            reasons.append("image too dark")
        if mean > 220:
            reasons.append("image overexposed")
        return {
            "ok": not reasons,
            "reason": "OK auto capture ready" if not reasons else " / ".join(reasons[:2]),
            "features": features,
            "metrics": {
                "blur": round(blur, 1),
                "threshold": round(threshold, 1),
                "focus": round(focus_score(gray), 1),
                "mean": round(mean, 1),
            },
        }

    def capture(self, image, overlay, quality, forced=False):
        idx = self.next_index
        self.next_index += 1
        image_path = self.images_dir / f"calib_{idx:03d}.jpg"
        overlay_path = self.overlays_dir / f"calib_{idx:03d}_overlay.jpg"
        cv2.imwrite(str(image_path), image, [int(cv2.IMWRITE_JPEG_QUALITY), 96])
        cv2.imwrite(str(overlay_path), overlay, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        sample = {
            "index": idx,
            "image": str(image_path),
            "overlay": str(overlay_path),
            "forced": bool(forced),
            "features": quality.get("features") or {},
            "metrics": quality.get("metrics") or {},
        }
        self.samples.append(sample)
        (self.output_dir / "manifest.json").write_text(json.dumps({"samples": self.samples}, ensure_ascii=False, indent=2), encoding="utf-8")
        self.last_capture_time = time.time()
        self.ok_since = None
        self.status_message = f"Captured {idx}/{self.args.target_count}"
        print(self.status_message, flush=True)

    def should_capture(self, quality):
        if self.solved or not self.auto_capture or not quality["ok"]:
            self.ok_since = None
            return False
        now = time.time()
        if self.ok_since is None:
            self.ok_since = now
        return now - self.ok_since >= 0.7 and now - self.last_capture_time >= 1.3

    def solve(self):
        if self.solved:
            return
        self.status_message = "Solving calibration..."
        print(self.status_message, flush=True)
        result = solve_from_images(self.images_dir, self.output_dir, self.pattern_size, self.args.square_size, self.args.min_count)
        if result.get("ok"):
            self.solved = True
            self.auto_capture = False
            self.status_message = f"Solved RMS {result['rms_px']:.4f}px"
            print(self.status_message, flush=True)
            print(f"Output: {self.output_dir}", flush=True)
        else:
            self.status_message = result.get("message", "Solve failed")
            print(self.status_message, flush=True)

    def reset(self):
        for folder in (self.images_dir, self.overlays_dir):
            for path in folder.glob("*"):
                if path.is_file():
                    path.unlink()
        self.samples = []
        self.next_index = 1
        self.solved = False
        self.auto_capture = True
        self.status_message = "Reset complete"

    def current_tune_param(self):
        return self.tune_params[self.tune_index % len(self.tune_params)]

    def apply_tuning(self, name):
        value = self.tune_values[name]
        if self.camera is not None and hasattr(self.camera, "set_tuning"):
            ok = self.camera.set_tuning(name, value)
            self.status_message = f"{name}={value:g}" if ok else f"{name} unsupported"
        else:
            self.status_message = f"{name}={value:g}"

    def tune_step(self, direction, fast=False):
        name, _label, lower, upper, step = self.current_tune_param()
        delta = step * (5.0 if fast else 1.0) * direction
        value = clamp(self.tune_values[name] + delta, lower, upper)
        if step < 1.0:
            value = round(value, 3)
        else:
            value = round(value)
        self.tune_values[name] = value
        self.apply_tuning(name)

    def draw_buttons(self, canvas):
        h, w = canvas.shape[:2]
        if self.tune_mode:
            name, label, _lower, _upper, _step = self.current_tune_param()
            value = self.tune_values[name]
            labels = [
                ("tune_param", f"{label}: {value:g}"),
                ("tune_prev", "Prev"),
                ("tune_down_fast", "--"),
                ("tune_down", "-"),
                ("tune_up", "+"),
                ("tune_up_fast", "++"),
                ("tune_done", "Done"),
            ]
        else:
            labels = [
                ("auto", "Pause Auto" if self.auto_capture else "Resume Auto"),
                ("focus", "Full View" if self.focus_view else "Focus View"),
                ("save", "Save Now"),
                ("tune", "Tune"),
                ("solve", "Solve Now"),
                ("reset", "Reset"),
                ("quit", "Quit"),
            ]
        margin, gap, button_h = 12, 10, 54
        y1 = h - button_h - margin
        button_w = int((w - margin * 2 - gap * (len(labels) - 1)) / len(labels))
        self.buttons = []
        for i, (name, label) in enumerate(labels):
            x1, y2 = margin + i * (button_w + gap), y1 + button_h
            x2 = x1 + button_w
            cv2.rectangle(canvas, (x1, y1), (x2, y2), (45, 85, 110), -1)
            cv2.putText(canvas, label, (x1 + 12, y1 + 35), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
            self.buttons.append((name, x1, y1, x2, y2))

    def draw(self, image, quality):
        h, w = image.shape[:2]
        side_w = 390
        canvas = np.zeros((h + 78, w + side_w, 3), dtype=np.uint8)
        canvas[:] = (24, 29, 35)
        canvas[:h, :w] = image
        side = canvas[:h, w:]
        side[:] = (248, 250, 252)
        ok = bool(quality and quality.get("ok")) and not self.solved
        cv2.rectangle(side, (18, 18), (side_w - 18, 86), (25, 128, 65) if ok else (35, 35, 180), -1)
        cv2.putText(side, "SOLVED" if self.solved else ("OK" if ok else "NO"), (38, 66), cv2.FONT_HERSHEY_SIMPLEX, 1.35, (255, 255, 255), 3)
        metrics = quality.get("metrics", {}) if quality else {}
        lines = [
            f"Images: {len(self.samples)}/{self.args.target_count}",
            f"Auto: {'ON' if self.auto_capture else 'OFF'}",
            f"Blur: {metrics.get('blur', '-')} / {metrics.get('threshold', '-')}",
            f"Focus: {metrics.get('focus', '-')}",
            f"Mean: {metrics.get('mean', '-')}",
            f"Output: {self.output_dir}",
        ]
        if self.tune_mode:
            name, label, _lower, _upper, _step = self.current_tune_param()
            lines.insert(2, f"Tune: {label}={self.tune_values[name]:g}")
        y = 130
        for line in lines:
            cv2.putText(side, line, (22, y), cv2.FONT_HERSHEY_SIMPLEX, 0.56, (30, 40, 50), 1, cv2.LINE_AA)
            y += 34
        reason = (quality or {}).get("reason") or self.status_message
        cv2.putText(side, reason[:34], (22, h - 48), cv2.FONT_HERSHEY_SIMPLEX, 0.56, (20, 30, 40), 1, cv2.LINE_AA)
        cv2.putText(canvas, "Move checkerboard. Auto capture and auto solve are enabled.", (16, h + 26), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (230, 238, 245), 1)
        self.draw_buttons(canvas)
        return canvas

    def build_preview(self, overlay):
        h, w = overlay.shape[:2]
        scale = min(1.0, self.args.preview_width / max(1, w), self.args.preview_height / max(1, h))
        preview = cv2.resize(overlay, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA) if scale < 1.0 else overlay.copy()
        if not self.focus_view:
            return preview

        ph, pw = preview.shape[:2]
        roi_w, roi_h = int(pw * 0.34), int(ph * 0.34)
        x1, y1 = (pw - roi_w) // 2, (ph - roi_h) // 2
        x2, y2 = x1 + roi_w, y1 + roi_h
        cv2.rectangle(preview, (x1, y1), (x2, y2), (0, 220, 255), 2)

        inset = preview[y1:y2, x1:x2]
        inset_w, inset_h = min(300, max(160, pw // 3)), min(240, max(120, ph // 3))
        inset = cv2.resize(inset, (inset_w, inset_h), interpolation=cv2.INTER_LINEAR)
        ix2, iy2 = pw - 12, ph - 12
        ix1, iy1 = ix2 - inset_w, iy2 - inset_h
        if ix1 >= 0 and iy1 >= 0:
            preview[iy1 - 3 : iy2 + 3, ix1 - 3 : ix2 + 3] = (20, 20, 20)
            preview[iy1:iy2, ix1:ix2] = inset
            cv2.rectangle(preview, (ix1, iy1), (ix2, iy2), (0, 220, 255), 2)
            cv2.putText(preview, "FOCUS", (ix1 + 8, iy1 + 24), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 220, 255), 2, cv2.LINE_AA)
        return preview

    def handle_click(self, event, x, y, flags, param):
        if event != cv2.EVENT_LBUTTONUP:
            return
        for name, x1, y1, x2, y2 in self.buttons:
            if x1 <= x <= x2 and y1 <= y <= y2:
                if name == "tune_param":
                    self.tune_index = (self.tune_index + 1) % len(self.tune_params)
                elif name == "tune_prev":
                    self.tune_index = (self.tune_index - 1) % len(self.tune_params)
                elif name == "tune_down_fast":
                    self.tune_step(-1, fast=True)
                elif name == "tune_down":
                    self.tune_step(-1)
                elif name == "tune_up":
                    self.tune_step(1)
                elif name == "tune_up_fast":
                    self.tune_step(1, fast=True)
                elif name == "tune_done":
                    self.tune_mode = False
                elif name == "auto":
                    self.auto_capture = not self.auto_capture
                elif name == "focus":
                    self.focus_view = not self.focus_view
                elif name == "save":
                    if self.current_frame is not None and self.last_found:
                        quality = self.current_quality or {"features": {}, "metrics": {}, "ok": False}
                        self.capture(self.current_frame, self.current_overlay, quality, forced=True)
                    else:
                        self.status_message = "No checkerboard to save"
                elif name == "tune":
                    self.tune_mode = True
                elif name == "solve":
                    if len(self.samples) >= self.args.min_count:
                        self.solve()
                elif name == "reset":
                    self.reset()
                elif name == "quit":
                    self.quit_requested = True

    def run(self):
        cap = self.open_camera()
        self.camera = cap
        cv2.namedWindow(self.args.window_name, cv2.WINDOW_NORMAL)
        cv2.setMouseCallback(self.args.window_name, self.handle_click)
        delay = max(1, int(1000 / max(self.args.preview_hz, 1)))
        print(f"Output: {self.output_dir}", flush=True)
        while not self.quit_requested:
            ok, frame = cap.read()
            if not ok or frame is None:
                self.status_message = "No camera frame"
                time.sleep(0.1)
                continue
            now = time.time()
            detected = self.last_quality is None or now - self.last_detection_time >= 1.0 / max(self.args.detect_hz, 0.1)
            if detected:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                h, w = gray.shape[:2]
                scale = min(1.0, self.args.detect_width / max(1, w))
                detect_gray = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA) if scale < 1.0 else gray
                found, corners = find_corners(detect_gray, self.pattern_size)
                if found and scale < 1.0:
                    corners = corners / scale
                quality = self.quality(frame, gray, corners if found else None)
                self.last_detection_time = now
                self.last_found, self.last_corners, self.last_quality = found, corners, quality
            else:
                found, corners, quality = self.last_found, self.last_corners, self.last_quality
            overlay = frame.copy()
            if found:
                cv2.drawChessboardCorners(overlay, self.pattern_size, corners, found)
            self.current_frame = frame.copy()
            self.current_overlay = overlay.copy()
            self.current_quality = quality
            if detected and self.should_capture(quality):
                self.capture(frame, overlay, quality)
            if not self.solved and len(self.samples) >= self.args.target_count:
                self.solve()
            preview = self.build_preview(overlay)
            cv2.imshow(self.args.window_name, self.draw(preview, quality))
            key = cv2.waitKey(delay) & 0xFF
            if key in (ord("q"), ord("Q"), 27):
                break
            if key in (ord("a"), ord("A")):
                self.auto_capture = not self.auto_capture
            elif key in (ord("f"), ord("F")):
                self.focus_view = not self.focus_view
            elif key in (ord("c"), ord("C")) and len(self.samples) >= self.args.min_count:
                self.solve()
            elif key in (ord("r"), ord("R")):
                self.reset()
        cap.release()
        cv2.destroyAllWindows()


def main():
    default_output = pathlib.Path.home() / "fast_livo2_data" / "calib" / "camera_intrinsics" / "jr_usb" / time.strftime("%Y%m%d-%H%M%S")
    parser = argparse.ArgumentParser()
    parser.add_argument("--camera-index", type=int, default=0)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=1024)
    parser.add_argument("--fps", type=float, default=10)
    parser.add_argument("--output-dir", default=str(default_output))
    parser.add_argument("--inner-corners", default="11x8")
    parser.add_argument("--square-size", type=float, default=0.025)
    parser.add_argument("--target-count", type=int, default=40)
    parser.add_argument("--min-count", type=int, default=25)
    parser.add_argument("--preview-hz", type=float, default=5)
    parser.add_argument("--preview-width", type=int, default=960)
    parser.add_argument("--preview-height", type=int, default=700)
    parser.add_argument("--detect-hz", type=float, default=2)
    parser.add_argument("--detect-width", type=int, default=900)
    parser.add_argument("--min-sharpness", type=float, default=75.0)
    parser.add_argument("--min-sharpness-floor", type=float, default=45.0)
    parser.add_argument("--window-name", default="JR USB Camera Calibration")
    parser.add_argument("--focus-view", action="store_true")
    parser.add_argument("--duplicate-center", type=float, default=0.06)
    parser.add_argument("--duplicate-scale", type=float, default=0.025)
    parser.add_argument("--duplicate-tilt", type=float, default=0.08)
    UsbCalibrationApp(parser.parse_args()).run()


if __name__ == "__main__":
    main()
