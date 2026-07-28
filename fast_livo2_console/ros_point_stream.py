#!/usr/bin/env python3
import argparse
import json
import math
import struct
import sys
import time

import rospy
from nav_msgs.msg import Odometry, Path
from sensor_msgs.msg import Imu, PointCloud2
import sensor_msgs.point_cloud2 as pc2

try:
    from livox_ros_driver2.msg import CustomMsg
except Exception:
    CustomMsg = None


class Streamer:
    def __init__(self, mode, max_points, hz, voxel_size):
        self.mode = mode
        self.max_points = max_points
        self.min_period = 1.0 / max(hz, 0.1)
        self.voxel_size = max(0.0, voxel_size)
        self.last_emit = {}
        self.counts = {}
        self.last_rates = None
        self.path_points = []
        self.last_lidar = 0.0
        self.last_imu = 0.0
        self.lidar_points = 0

    def write(self, obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def tick(self, topic):
        now = time.time()
        bucket = self.counts.setdefault(topic, {"count": 0, "last": now})
        bucket["count"] += 1
        if self.last_rates is None:
            self.last_rates = now
            return
        if now - self.last_rates >= 1.0:
            rates = {}
            for key, value in self.counts.items():
                elapsed = max(0.001, now - value["last"])
                rates[key] = round(value["count"] / elapsed, 2)
                value["count"] = 0
                value["last"] = now
            self.last_rates = now
            self.write({
                "type": "rates",
                "rates": rates,
                "health": {
                    "lidar_points": self.lidar_points,
                    "lidar_age": round(max(0.0, now - self.last_lidar), 2) if self.last_lidar else None,
                    "imu_age": round(max(0.0, now - self.last_imu), 2) if self.last_imu else None,
                },
            })

    def should_emit(self, key):
        now = time.time()
        if now - self.last_emit.get(key, 0) < self.min_period:
            return False
        self.last_emit[key] = now
        return True

    def sample_step(self, total):
        if total <= self.max_points:
            return 1
        return max(1, int(math.ceil(total / float(self.max_points))))

    @staticmethod
    def unpack_rgb(value):
        if value is None:
            return None
        try:
            if isinstance(value, float):
                packed = struct.pack("<f", value)
                rgb = struct.unpack("<I", packed)[0]
            else:
                rgb = int(value)
            return [(rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255]
        except Exception:
            return None

    @staticmethod
    def pseudo_color(value, mode):
        if mode == "lidar":
            t = max(0, min(255, int(value or 0)))
            return [35, min(255, 145 + t // 2), min(255, 185 + t // 3)]
        z = float(value or 0.0)
        t = max(0.0, min(1.0, (z + 1.5) / 4.0))
        return [int(60 + 160 * t), int(190 - 90 * t), int(255 - 130 * t)]

    def voxel_filter(self, rows):
        if self.voxel_size <= 0 or not rows:
            return rows
        seen = set()
        out = []
        inv = 1.0 / self.voxel_size
        for row in rows:
            key = (int(row[0] * inv), int(row[1] * inv), int(row[2] * inv))
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
        return out

    def livox_cb(self, msg):
        topic = "/livox/lidar"
        self.last_lidar = time.time()
        self.lidar_points = len(msg.points)
        self.tick(topic)
        if self.mode != "lidar" or not self.should_emit(topic):
            return
        total = len(msg.points)
        step = self.sample_step(total)
        points = []
        for p in msg.points[::step]:
            intensity = getattr(p, "reflectivity", 0)
            r, g, b = self.pseudo_color(intensity, "lidar")
            points.append([round(p.x, 3), round(p.y, 3), round(p.z, 3), r, g, b])
        points = self.voxel_filter(points)
        self.write({
            "type": "points",
            "mode": "lidar",
            "topic": topic,
            "frame": getattr(msg.header, "frame_id", ""),
            "stamp": time.time(),
            "raw_count": total,
            "count": len(points),
            "sampled_count": len(points),
            "has_rgb": False,
            "rgb_status": "pseudo_lidar",
            "points": points,
        })

    def cloud_cb(self, msg):
        topic = "/cloud_registered"
        self.tick(topic)
        if self.mode != "mapping" or not self.should_emit(topic):
            return
        fields = [field.name for field in msg.fields]
        rgb_field = "rgb" if "rgb" in fields else "rgba" if "rgba" in fields else None
        field_names = ("x", "y", "z", rgb_field) if rgb_field else ("x", "y", "z")
        raw = list(pc2.read_points(msg, field_names=field_names, skip_nans=True))
        step = self.sample_step(len(raw))
        points = []
        rgb_hits = 0
        for p in raw[::step]:
            x, y, z = p[0], p[1], p[2]
            rgb = self.unpack_rgb(p[3]) if rgb_field else None
            if rgb is not None:
                rgb_hits += 1
                r, g, b = rgb
            else:
                r, g, b = self.pseudo_color(z, "mapping")
            points.append([round(x, 3), round(y, 3), round(z, 3), int(r), int(g), int(b)])
        points = self.voxel_filter(points)
        has_rgb = bool(rgb_field and rgb_hits > 0)
        self.write({
            "type": "points",
            "mode": "mapping",
            "topic": topic,
            "frame": getattr(msg.header, "frame_id", ""),
            "stamp": time.time(),
            "raw_count": len(raw),
            "count": len(points),
            "sampled_count": len(points),
            "has_rgb": has_rgb,
            "rgb_status": "rgb" if has_rgb else "missing_rgb_field" if not rgb_field else "rgb_decode_failed",
            "points": points,
        })

    def imu_cb(self, msg):
        self.last_imu = time.time()
        self.tick("/livox/imu")

    def path_cb(self, msg):
        self.tick("/path")
        poses = msg.poses[-600:]
        self.path_points = [
            [round(p.pose.position.x, 3), round(p.pose.position.y, 3), round(p.pose.position.z, 3)]
            for p in poses
        ]
        if self.mode == "mapping":
            payload = {"type": "path", "topic": "/path", "points": self.path_points}
            if poses:
                q = poses[-1].pose.orientation
                payload["orientation"] = [round(q.x, 6), round(q.y, 6), round(q.z, 6), round(q.w, 6)]
                payload["yaw"] = round(self.quaternion_to_yaw(q.x, q.y, q.z, q.w), 6)
            self.write(payload)

    def odom_cb(self, msg):
        self.tick("/aft_mapped_to_init")
        p = msg.pose.pose.position
        q = msg.pose.pose.orientation
        self.write({
            "type": "odom",
            "topic": "/aft_mapped_to_init",
            "position": [round(p.x, 3), round(p.y, 3), round(p.z, 3)],
            "orientation": [round(q.x, 6), round(q.y, 6), round(q.z, 6), round(q.w, 6)],
            "yaw": round(self.quaternion_to_yaw(q.x, q.y, q.z, q.w), 6),
        })

    @staticmethod
    def quaternion_to_yaw(x, y, z, w):
        siny_cosp = 2.0 * (w * z + x * y)
        cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
        return math.atan2(siny_cosp, cosy_cosp)

    def subscribe(self):
        self.write({"type": "status", "level": "info", "message": "ROS stream connected"})
        rospy.Subscriber("/livox/imu", Imu, self.imu_cb, queue_size=50)
        if CustomMsg is not None:
            rospy.Subscriber("/livox/lidar", CustomMsg, self.livox_cb, queue_size=4)
        else:
            self.write({"type": "status", "level": "warn", "message": "livox_ros_driver2/CustomMsg import failed"})
        rospy.Subscriber("/cloud_registered", PointCloud2, self.cloud_cb, queue_size=2)
        rospy.Subscriber("/path", Path, self.path_cb, queue_size=2)
        rospy.Subscriber("/aft_mapped_to_init", Odometry, self.odom_cb, queue_size=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["health", "lidar", "mapping"], default="lidar")
    parser.add_argument("--max-points", type=int, default=12000)
    parser.add_argument("--hz", type=float, default=4.0)
    parser.add_argument("--voxel-size", type=float, default=0.0)
    args = parser.parse_args()

    rospy.init_node(f"fast_livo2_console_stream_{args.mode}", anonymous=True, disable_signals=True)
    streamer = Streamer(args.mode, args.max_points, args.hz, args.voxel_size)
    streamer.subscribe()
    rate = rospy.Rate(2)
    while not rospy.is_shutdown():
        rate.sleep()


if __name__ == "__main__":
    main()
