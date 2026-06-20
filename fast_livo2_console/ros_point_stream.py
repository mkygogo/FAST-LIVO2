#!/usr/bin/env python3
import argparse
import json
import math
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
    def __init__(self, mode, max_points, hz):
        self.mode = mode
        self.max_points = max_points
        self.min_period = 1.0 / max(hz, 0.1)
        self.last_emit = {}
        self.counts = {}
        self.last_rates = time.time()
        self.path_points = []

    def write(self, obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def tick(self, topic):
        now = time.time()
        bucket = self.counts.setdefault(topic, {"count": 0, "last": now, "hz": 0.0})
        bucket["count"] += 1
        elapsed = now - bucket["last"]
        if elapsed >= 1.0:
            bucket["hz"] = bucket["count"] / elapsed
            bucket["count"] = 0
            bucket["last"] = now
        if now - self.last_rates >= 1.0:
            self.last_rates = now
            self.write({"type": "rates", "rates": {k: round(v["hz"], 2) for k, v in self.counts.items()}})

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

    def livox_cb(self, msg):
        topic = "/livox/lidar"
        self.tick(topic)
        if self.mode != "lidar" or not self.should_emit(topic):
            return
        total = len(msg.points)
        step = self.sample_step(total)
        points = []
        for p in msg.points[::step]:
            intensity = getattr(p, "reflectivity", 0)
            points.append([round(p.x, 3), round(p.y, 3), round(p.z, 3), int(intensity)])
        self.write({
            "type": "points",
            "mode": "lidar",
            "topic": topic,
            "frame": getattr(msg.header, "frame_id", ""),
            "stamp": time.time(),
            "raw_count": total,
            "count": len(points),
            "points": points,
        })

    def cloud_cb(self, msg):
        topic = "/cloud_registered"
        self.tick(topic)
        if self.mode != "mapping" or not self.should_emit(topic):
            return
        raw = list(pc2.read_points(msg, field_names=("x", "y", "z"), skip_nans=True))
        step = self.sample_step(len(raw))
        points = [[round(p[0], 3), round(p[1], 3), round(p[2], 3), 80] for p in raw[::step]]
        self.write({
            "type": "points",
            "mode": "mapping",
            "topic": topic,
            "frame": getattr(msg.header, "frame_id", ""),
            "stamp": time.time(),
            "raw_count": len(raw),
            "count": len(points),
            "points": points,
        })

    def imu_cb(self, msg):
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
    parser.add_argument("--mode", choices=["lidar", "mapping"], default="lidar")
    parser.add_argument("--max-points", type=int, default=12000)
    parser.add_argument("--hz", type=float, default=4.0)
    args = parser.parse_args()

    rospy.init_node(f"fast_livo2_console_stream_{args.mode}", anonymous=True, disable_signals=True)
    streamer = Streamer(args.mode, args.max_points, args.hz)
    streamer.subscribe()
    rate = rospy.Rate(2)
    while not rospy.is_shutdown():
        rate.sleep()


if __name__ == "__main__":
    main()
