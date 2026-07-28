#!/usr/bin/env python3
"""Touch-friendly live preview and rosbag recorder for FAST-Calib2."""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

import cv2
import numpy as np
import rospy
from cv_bridge import CvBridge
from sensor_msgs.msg import Image


WINDOW_NAME = "FAST-Calib2 Live Recording"
EXPECTED_MARKERS = {1, 2, 3, 4}
SCREEN_WIDTH = 1024
SCREEN_HEIGHT = 600
VIEW_WIDTH = 700
PANEL_X = VIEW_WIDTH


class PreviewRecorder:
    def __init__(self, args):
        self.args = args
        self.bridge = CvBridge()
        self.lock = threading.Lock()
        self.latest_image = None
        self.latest_stamp = 0.0
        self.detected_ids = set()
        self.safe_framing = False
        self.brightness = 0.0
        self.record_process = None
        self.record_log = None
        self.scene_dir = None
        self.record_started = 0.0
        self.record_finished = False
        self.record_error = ""
        self.lidar_validation_status = "idle"
        self.lidar_validation_complete = False
        self.lidar_center_count = None
        self.lidar_validation_message = ""
        self.lidar_validation_thread = None
        self.force_until = 0.0
        self.exit_requested = False
        self.aruco_dictionary = cv2.aruco.Dictionary_get(cv2.aruco.DICT_6X6_250)
        self.aruco_parameters = cv2.aruco.DetectorParameters_create()

    def image_callback(self, msg):
        try:
            image = self.bridge.imgmsg_to_cv2(msg, desired_encoding="bgr8")
        except Exception as exc:
            rospy.logwarn_throttle(5.0, "FAST-Calib2 preview conversion failed: %s", exc)
            return
        with self.lock:
            self.latest_image = image
            self.latest_stamp = time.monotonic()

    def get_image(self):
        with self.lock:
            if self.latest_image is None:
                return None, self.latest_stamp
            return self.latest_image.copy(), self.latest_stamp

    def published_topics(self):
        try:
            return {name for name, _ in rospy.get_published_topics()}
        except rospy.ROSException:
            return set()

    def start_recording(self, image):
        if self.record_process is not None or self.record_finished:
            return
        self.lidar_validation_status = "idle"
        self.lidar_validation_complete = False
        self.lidar_center_count = None
        self.lidar_validation_message = ""
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.scene_dir = os.path.join(
            self.args.data_root, "datasets", "%s-scene-%s" % (timestamp, timestamp)
        )
        os.makedirs(self.scene_dir, exist_ok=False)

        image_path = os.path.join(self.scene_dir, "image.png")
        if not cv2.imwrite(image_path, image):
            self.record_error = "Cannot save reference image"
            return
        intrinsics_copy = os.path.join(self.scene_dir, "camera_intrinsics_fast_calib2.yaml")
        if self.args.intrinsics and os.path.isfile(self.args.intrinsics):
            shutil.copy2(self.args.intrinsics, intrinsics_copy)

        topics = [self.args.image_topic, self.args.lidar_topic, self.args.imu_topic]
        if self.args.frame_info_topic in self.published_topics():
            topics.append(self.args.frame_info_topic)
        bag_path = os.path.join(self.scene_dir, "scene.bag")
        self.record_log = open(os.path.join(self.scene_dir, "rosbag-record.log"), "w")
        command = [
            "rosbag", "record", "--duration=%s" % self.args.duration,
            "--lz4", "-O", bag_path,
        ] + topics
        try:
            self.record_process = subprocess.Popen(
                command, stdout=self.record_log, stderr=subprocess.STDOUT
            )
        except OSError as exc:
            self.record_log.close()
            self.record_log = None
            self.record_error = "Cannot start rosbag: %s" % exc
            return
        self.record_started = time.monotonic()
        self.write_metadata(topics, bag_path, image_path, intrinsics_copy, "recording")

    def write_metadata(self, topics, bag_path, image_path, intrinsics_path, status):
        metadata = {
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "scene_name": os.path.basename(self.scene_dir),
            "duration_sec": self.args.duration,
            "status": status,
            "image_topic": self.args.image_topic,
            "lidar_topic": self.args.lidar_topic,
            "imu_topic": self.args.imu_topic,
            "frame_info_topic": self.args.frame_info_topic,
            "recorded_topics": topics,
            "image": image_path,
            "bag": bag_path,
            "intrinsics": intrinsics_path if os.path.isfile(intrinsics_path) else "",
            "lidar_validation": {
                "status": self.lidar_validation_status,
                "center_count": self.lidar_center_count,
                "message": self.lidar_validation_message,
                "log": (
                    os.path.join(self.scene_dir, "lidar_validation", "validation.log")
                    if self.scene_dir else ""
                ),
            },
        }
        with open(os.path.join(self.scene_dir, "metadata.json"), "w") as output:
            json.dump(metadata, output, ensure_ascii=False, indent=2)
            output.write("\n")

    def start_lidar_validation(self):
        if self.lidar_validation_status != "idle" or not self.scene_dir:
            return
        self.lidar_validation_status = "running"
        bag_path = os.path.join(self.scene_dir, "scene.bag")
        image_path = os.path.join(self.scene_dir, "image.png")
        intrinsics_path = os.path.join(
            self.scene_dir, "camera_intrinsics_fast_calib2.yaml"
        )
        topics = [self.args.image_topic, self.args.lidar_topic, self.args.imu_topic]
        if self.args.frame_info_topic in self.published_topics():
            topics.append(self.args.frame_info_topic)
        self.write_metadata(
            topics, bag_path, image_path, intrinsics_path, "validating_lidar"
        )

        def worker():
            validation_dir = os.path.join(self.scene_dir, "lidar_validation")
            os.makedirs(validation_dir, exist_ok=True)
            log_path = os.path.join(validation_dir, "validation.log")
            output_text = ""
            try:
                if not os.path.isfile(self.args.lidar_validator_config):
                    raise RuntimeError(
                        "validator config missing: %s" % self.args.lidar_validator_config
                    )
                subprocess.run(
                    ["rosparam", "load", self.args.lidar_validator_config],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=8,
                )
                subprocess.run(
                    ["rosparam", "set", "output_path", validation_dir],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=5,
                )
                result = subprocess.run(
                    [
                        "rosrun", "fast_calib", "lidar_center_test",
                        bag_path, self.args.lidar_topic, "solid",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=self.args.lidar_validator_timeout,
                )
                output_text = result.stdout or ""
                match = re.search(
                    r"\[LiDAR Test\] Raw center count:\s*(\d+)", output_text
                )
                self.lidar_center_count = int(match.group(1)) if match else None
                candidate_match = re.search(
                    r"Unable to select 4 geometry-consistent annulus centers "
                    r"from\s+(\d+)\s+candidates",
                    output_text,
                )
                if (self.lidar_center_count in (None, 0)
                        and candidate_match is not None):
                    self.lidar_center_count = int(candidate_match.group(1))
                if result.returncode == 0 and self.lidar_center_count == 4:
                    self.lidar_validation_status = "passed"
                    self.lidar_validation_message = "four circles detected"
                    metadata_status = "complete"
                else:
                    self.lidar_validation_status = "failed"
                    if self.lidar_center_count is None:
                        self.lidar_validation_message = "circle detection failed"
                    else:
                        self.lidar_validation_message = "%d of 4 circles detected" % (
                            self.lidar_center_count
                        )
                    metadata_status = "invalid_lidar"
            except subprocess.TimeoutExpired as exc:
                partial_output = exc.stdout or ""
                if isinstance(partial_output, bytes):
                    partial_output = partial_output.decode("utf-8", errors="replace")
                output_text = partial_output + "\nLiDAR validation timed out\n"
                self.lidar_validation_status = "error"
                self.lidar_validation_message = "validation timed out"
                metadata_status = "validation_error"
            except Exception as exc:
                output_text += "\nLiDAR validation error: %s\n" % exc
                self.lidar_validation_status = "error"
                self.lidar_validation_message = str(exc)
                metadata_status = "validation_error"

            with open(log_path, "w") as output:
                output.write(output_text)
            self.write_metadata(
                topics, bag_path, image_path, intrinsics_path, metadata_status
            )
            self.lidar_validation_complete = True

        self.lidar_validation_thread = threading.Thread(target=worker, daemon=True)
        self.lidar_validation_thread.start()

    def finish_recording(self):
        if self.record_process is None:
            return
        return_code = self.record_process.poll()
        if return_code is None:
            return
        if self.record_log:
            self.record_log.close()
            self.record_log = None
        bag_path = os.path.join(self.scene_dir, "scene.bag")
        topics = [self.args.image_topic, self.args.lidar_topic, self.args.imu_topic]
        if self.args.frame_info_topic in self.published_topics():
            topics.append(self.args.frame_info_topic)
        image_path = os.path.join(self.scene_dir, "image.png")
        intrinsics_path = os.path.join(self.scene_dir, "camera_intrinsics_fast_calib2.yaml")
        if return_code == 0 and os.path.isfile(bag_path) and os.path.getsize(bag_path) > 1024 * 1024:
            self.record_finished = True
            self.start_lidar_validation()
        else:
            self.record_error = "rosbag failed (code %s)" % return_code
            self.write_metadata(topics, bag_path, image_path, intrinsics_path, "failed")
        self.record_process = None

    def reset_after_failed_validation(self):
        self.record_process = None
        self.record_finished = False
        self.record_error = ""
        self.scene_dir = None
        self.record_started = 0.0
        self.lidar_validation_status = "idle"
        self.lidar_validation_complete = False
        self.lidar_center_count = None
        self.lidar_validation_message = ""
        self.lidar_validation_thread = None
        self.force_until = 0.0

    def cancel_recording(self):
        if self.record_process is not None and self.record_process.poll() is None:
            self.record_process.send_signal(signal.SIGINT)
            try:
                self.record_process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.record_process.terminate()
        if self.record_log:
            self.record_log.close()
            self.record_log = None

    @staticmethod
    def put_text(canvas, text, origin, scale=0.65, color=(235, 235, 235), thickness=2):
        cv2.putText(canvas, text, origin, cv2.FONT_HERSHEY_SIMPLEX, scale, color,
                    thickness, cv2.LINE_AA)

    def analyse_and_draw_image(self, image):
        source_h, source_w = image.shape[:2]
        scale = min(VIEW_WIDTH / source_w, SCREEN_HEIGHT / source_h)
        draw_w = max(1, int(source_w * scale))
        draw_h = max(1, int(source_h * scale))
        resized = cv2.resize(image, (draw_w, draw_h), interpolation=cv2.INTER_AREA)
        offset_x = (VIEW_WIDTH - draw_w) // 2
        offset_y = (SCREEN_HEIGHT - draw_h) // 2

        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        corners, ids, _ = cv2.aruco.detectMarkers(
            gray, self.aruco_dictionary, parameters=self.aruco_parameters
        )
        self.detected_ids = set(int(value) for value in ids.flatten()) if ids is not None else set()
        self.brightness = float(np.mean(gray))

        margin_x = max(18, int(draw_w * 0.045))
        margin_y = max(18, int(draw_h * 0.045))
        safe_left, safe_top = margin_x, margin_y
        safe_right, safe_bottom = draw_w - margin_x, draw_h - margin_y
        wanted_corners = []
        if ids is not None:
            for marker_corners, marker_id in zip(corners, ids.flatten()):
                if int(marker_id) in EXPECTED_MARKERS:
                    wanted_corners.extend(marker_corners.reshape(-1, 2).tolist())
        self.safe_framing = False
        if EXPECTED_MARKERS.issubset(self.detected_ids) and wanted_corners:
            points = np.asarray(wanted_corners)
            self.safe_framing = bool(
                points[:, 0].min() >= safe_left and points[:, 0].max() <= safe_right
                and points[:, 1].min() >= safe_top and points[:, 1].max() <= safe_bottom
            )

        border_color = (70, 210, 80) if self.safe_framing else (0, 190, 255)
        cv2.rectangle(resized, (safe_left, safe_top), (safe_right, safe_bottom), border_color, 2)
        if ids is not None:
            cv2.aruco.drawDetectedMarkers(resized, corners, ids)
        return resized, offset_x, offset_y

    def draw_button(self, canvas, rect, label, color, enabled=True):
        x1, y1, x2, y2 = rect
        fill = color if enabled else (80, 80, 80)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), fill, -1)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (230, 230, 230), 2)
        size = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.72, 2)[0]
        tx = x1 + (x2 - x1 - size[0]) // 2
        ty = y1 + (y2 - y1 + size[1]) // 2
        self.put_text(canvas, label, (tx, ty), 0.72, (255, 255, 255), 2)

    def draw_panel(self, canvas, image_age):
        x = PANEL_X + 18
        self.put_text(canvas, "FAST-Calib2", (x, 42), 0.85, (255, 255, 255), 2)
        self.put_text(canvas, "LIVE CAMERA", (x, 72), 0.55, (150, 210, 255), 1)
        cv2.line(canvas, (x, 88), (SCREEN_WIDTH - 18, 88), (100, 100, 100), 1)

        marker_count = len(EXPECTED_MARKERS.intersection(self.detected_ids))
        marker_color = (80, 220, 90) if marker_count == 4 else (0, 180, 255)
        self.put_text(canvas, "Markers", (x, 126), 0.58)
        self.put_text(canvas, "%d / 4" % marker_count, (x + 172, 126), 0.72, marker_color, 2)

        if marker_count == 4 and self.safe_framing:
            frame_text, frame_color = "SAFE", (80, 220, 90)
        elif marker_count == 4:
            frame_text, frame_color = "MOVE BACK", (0, 180, 255)
        else:
            frame_text, frame_color = "NO BOARD", (0, 180, 255)
        self.put_text(canvas, "Board edge", (x, 165), 0.58)
        self.put_text(canvas, frame_text, (x + 140, 165), 0.56, frame_color, 2)

        if self.brightness < 35:
            light_text, light_color = "TOO DARK", (0, 120, 255)
        elif self.brightness > 225:
            light_text, light_color = "TOO BRIGHT", (0, 120, 255)
        else:
            light_text, light_color = "OK", (80, 220, 90)
        self.put_text(canvas, "Brightness", (x, 204), 0.58)
        self.put_text(canvas, "%s  %.0f" % (light_text, self.brightness),
                      (x + 132, 204), 0.50, light_color, 2)

        camera_ok = image_age < 1.0
        camera_color = (80, 220, 90) if camera_ok else (0, 80, 255)
        self.put_text(canvas, "Camera", (x, 243), 0.58)
        self.put_text(canvas, "LIVE" if camera_ok else "NO SIGNAL", (x + 172, 243),
                      0.56, camera_color, 2)

        cv2.line(canvas, (x, 265), (SCREEN_WIDTH - 18, 265), (100, 100, 100), 1)
        recommended = marker_count == 4 and self.safe_framing and 35 <= self.brightness <= 225
        can_record = marker_count >= 3 and image_age < 1.0
        now = time.monotonic()

        if self.record_process is not None:
            remaining = max(0.0, self.args.duration - (now - self.record_started))
            self.put_text(canvas, "REC", (x, 315), 1.0, (0, 0, 255), 3)
            self.put_text(canvas, "%.1f s" % remaining, (x + 100, 315), 0.95,
                          (255, 255, 255), 2)
            self.put_text(canvas, "Keep scene still", (x, 352), 0.55, (0, 200, 255), 2)
            self.draw_button(canvas, (x, 470, SCREEN_WIDTH - 18, 574), "RECORDING...",
                             (70, 70, 70), False)
        elif self.record_finished and not self.lidar_validation_complete:
            self.put_text(canvas, "CHECKING LIDAR", (x, 315), 0.68,
                          (0, 200, 255), 2)
            self.put_text(canvas, "Detecting 4 circles...", (x, 352), 0.48,
                          (255, 255, 255), 1)
            self.put_text(canvas, "Keep preview open", (x, 384), 0.48,
                          (180, 180, 180), 1)
            self.draw_button(canvas, (x, 470, SCREEN_WIDTH - 18, 574),
                             "PLEASE WAIT", (70, 70, 70), False)
        elif self.record_finished and self.lidar_validation_status == "passed":
            bag_path = os.path.join(self.scene_dir, "scene.bag")
            size_mb = os.path.getsize(bag_path) / (1024.0 * 1024.0)
            self.put_text(canvas, "VALID DATA", (x, 315), 0.82, (80, 220, 90), 2)
            self.put_text(canvas, "LiDAR circles  4 / 4", (x, 352), 0.53,
                          (80, 220, 90), 2)
            self.put_text(canvas, "Bag: %.0f MB" % size_mb, (x, 384), 0.55,
                          (255, 255, 255), 2)
            self.draw_button(canvas, (x, 470, SCREEN_WIDTH - 18, 574), "DONE / CLOSE",
                             (45, 145, 65), True)
        elif self.record_finished and self.lidar_validation_status in ("failed", "error"):
            count_text = (
                "%d / 4" % self.lidar_center_count
                if self.lidar_center_count is not None else "FAILED"
            )
            self.put_text(canvas, "INVALID DATA", (x, 315), 0.78, (0, 80, 255), 2)
            self.put_text(canvas, "LiDAR circles  %s" % count_text, (x, 352), 0.53,
                          (0, 140, 255), 2)
            self.put_text(canvas, "Adjust angle, then retry", (x, 384), 0.45,
                          (210, 210, 210), 1)
            self.draw_button(canvas, (x, 410, SCREEN_WIDTH - 18, 494), "RETRY",
                             (25, 125, 175), True)
            self.draw_button(canvas, (x, 505, SCREEN_WIDTH - 18, 574), "CLOSE",
                             (85, 85, 95), True)
        elif self.record_error:
            self.put_text(canvas, "RECORD FAILED", (x, 315), 0.72, (0, 70, 255), 2)
            self.put_text(canvas, self.record_error[:25], (x, 350), 0.42,
                          (0, 150, 255), 1)
            self.draw_button(canvas, (x, 470, SCREEN_WIDTH - 18, 574), "CLOSE",
                             (120, 65, 45), True)
        else:
            if recommended:
                status, status_color = "READY", (80, 220, 90)
                hint = "Hold scene still"
            elif can_record:
                status, status_color = "CHECK FRAMING", (0, 180, 255)
                hint = "Tap twice to force"
            else:
                status, status_color = "NOT READY", (0, 120, 255)
                hint = "Need at least 3 markers"
            self.put_text(canvas, status, (x, 315), 0.78, status_color, 2)
            self.put_text(canvas, hint, (x, 352), 0.52, (210, 210, 210), 1)
            seconds_label = "%gs" % self.args.duration
            label = ("START " if recommended else "RECORD ") + seconds_label
            color = (45, 145, 65) if recommended else (25, 125, 175)
            self.draw_button(canvas, (x, 410, SCREEN_WIDTH - 18, 494), label,
                             color, can_record)
            self.draw_button(canvas, (x, 505, SCREEN_WIDTH - 18, 574), "CANCEL",
                             (85, 85, 95), True)
        return recommended, can_record

    def on_mouse(self, event, mouse_x, mouse_y, _flags, _userdata):
        # Touchscreens do not always deliver a clean button-up event when the
        # finger moves by a few pixels. Start actions on button-down instead;
        # state changes below make the following synthetic/up event harmless.
        if event != cv2.EVENT_LBUTTONDOWN or mouse_x < PANEL_X:
            return
        print(
            "[FAST-Calib2 touch] x=%d y=%d recording=%s finished=%s "
            "validation=%s markers=%d safe=%s brightness=%.1f"
            % (
                mouse_x,
                mouse_y,
                self.record_process is not None,
                self.record_finished,
                self.lidar_validation_status,
                len(EXPECTED_MARKERS.intersection(self.detected_ids)),
                self.safe_framing,
                self.brightness,
            ),
            flush=True,
        )
        if (self.record_finished and self.lidar_validation_complete
                and self.lidar_validation_status == "passed"):
            if 470 <= mouse_y <= 590:
                self.exit_requested = True
            return
        if (self.record_finished and self.lidar_validation_complete
                and self.lidar_validation_status in ("failed", "error")):
            if 410 <= mouse_y <= 500:
                self.reset_after_failed_validation()
            elif 505 <= mouse_y <= 590:
                self.exit_requested = True
            return
        if self.record_finished:
            return
        if self.record_error:
            if 470 <= mouse_y <= 590:
                self.exit_requested = True
            return
        if self.record_process is not None:
            return
        if 505 <= mouse_y <= 590:
            self.exit_requested = True
            return
        if not (410 <= mouse_y <= 500):
            return
        image, stamp = self.get_image()
        if image is None or time.monotonic() - stamp >= 1.0:
            return
        marker_count = len(EXPECTED_MARKERS.intersection(self.detected_ids))
        recommended = marker_count == 4 and self.safe_framing and 35 <= self.brightness <= 225
        if marker_count < 3:
            return
        now = time.monotonic()
        if not recommended and now > self.force_until:
            self.force_until = now + 3.0
            return
        self.start_recording(image)

    def run(self):
        rospy.init_node("jr_fast_calib2_record_preview", anonymous=True, disable_signals=True)
        rospy.Subscriber(self.args.image_topic, Image, self.image_callback,
                         queue_size=1, buff_size=32 * 1024 * 1024)
        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
        cv2.setWindowProperty(WINDOW_NAME, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        cv2.setMouseCallback(WINDOW_NAME, self.on_mouse)

        wait_deadline = time.monotonic() + 12.0
        while not self.exit_requested and not rospy.is_shutdown():
            image, stamp = self.get_image()
            canvas = np.full((SCREEN_HEIGHT, SCREEN_WIDTH, 3), 24, dtype=np.uint8)
            if image is None:
                self.put_text(canvas, "Waiting for camera image...", (120, 290), 1.0,
                              (0, 180, 255), 2)
                if time.monotonic() > wait_deadline:
                    self.record_error = "No camera image"
            else:
                preview, offset_x, offset_y = self.analyse_and_draw_image(image)
                h, w = preview.shape[:2]
                canvas[offset_y:offset_y + h, offset_x:offset_x + w] = preview
            image_age = time.monotonic() - stamp if stamp else 999.0
            self.draw_panel(canvas, image_age)
            self.finish_recording()
            cv2.imshow(WINDOW_NAME, canvas)
            key = cv2.waitKey(20) & 0xFF
            if (key in (27, ord("q")) and self.record_process is None
                    and (not self.record_finished or self.lidar_validation_complete)):
                self.exit_requested = True
        self.cancel_recording()
        cv2.destroyAllWindows()
        if (self.record_finished and self.lidar_validation_complete
                and self.lidar_validation_status == "passed"):
            return 0
        return 1 if self.record_error or self.record_finished else 2


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--intrinsics", default="")
    parser.add_argument("--duration", type=float, default=3.0)
    parser.add_argument("--image-topic", default="/left_camera/image")
    parser.add_argument("--lidar-topic", default="/livox/lidar")
    parser.add_argument("--imu-topic", default="/livox/imu")
    parser.add_argument("--frame-info-topic", default="/hikrobot_camera/frame_info")
    parser.add_argument(
        "--lidar-validator-config",
        default="/home/jr/fast_livo2_ws/src/FAST-Calib2/config/qr_params.yaml",
    )
    parser.add_argument("--lidar-validator-timeout", type=float, default=25.0)
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(PreviewRecorder(parse_args()).run())
