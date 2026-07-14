#!/usr/bin/env python3
import argparse
import base64
import json
import sys
import time

import cv2
import numpy as np
import rospy
from sensor_msgs.msg import Image

try:
    from cv_bridge import CvBridge
except Exception:
    CvBridge = None


class ImageStreamer:
    def __init__(self, topics, hz, width, quality):
        self.topics = topics
        self.min_period = 1.0 / max(hz, 0.1)
        self.width = max(160, width)
        self.quality = max(35, min(95, quality))
        self.bridge = CvBridge() if CvBridge is not None else None
        self.last_emit = 0.0
        self.active_topic = ""
        self.counts = {}
        self.last_rates = time.time()
        self.started = time.time()
        self.last_image = 0.0
        self.last_warn = 0.0

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

    def should_emit(self, topic):
        now = time.time()
        preferred = self.topics[0] if self.topics else topic
        if self.active_topic and topic != self.active_topic and self.active_topic == preferred:
            return False
        if now - self.last_emit < self.min_period:
            return False
        self.last_emit = now
        self.active_topic = topic
        return True

    def msg_to_bgr(self, msg):
        if self.bridge is not None:
            if msg.encoding in ("rgb8", "rgba8"):
                rgb = self.bridge.imgmsg_to_cv2(msg, desired_encoding="rgb8")
                return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            return self.bridge.imgmsg_to_cv2(msg, desired_encoding="bgr8")

        data = np.frombuffer(msg.data, dtype=np.uint8)
        if msg.encoding == "rgb8":
            rgb = data.reshape((msg.height, msg.width, 3))
            return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        if msg.encoding == "bgr8":
            return data.reshape((msg.height, msg.width, 3))
        if msg.encoding in ("mono8", "8UC1"):
            mono = data.reshape((msg.height, msg.width))
            return cv2.cvtColor(mono, cv2.COLOR_GRAY2BGR)
        raise ValueError("unsupported image encoding: %s" % msg.encoding)

    def image_cb(self, topic, msg):
        self.tick(topic)
        if not self.should_emit(topic):
            return
        try:
            self.last_image = time.time()
            img = self.msg_to_bgr(msg)
            scale = self.width / float(img.shape[1])
            if scale < 1.0:
                img = cv2.resize(img, (self.width, max(1, int(img.shape[0] * scale))), interpolation=cv2.INTER_AREA)
            ok, encoded = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
            if not ok:
                raise RuntimeError("jpeg encode failed")
            self.write({
                "type": "image",
                "topic": topic,
                "frame": getattr(msg.header, "frame_id", ""),
                "stamp": time.time(),
                "width": int(img.shape[1]),
                "height": int(img.shape[0]),
                "encoding": "jpeg",
                "data": base64.b64encode(encoded.tobytes()).decode("ascii"),
            })
        except Exception as exc:
            self.write({"type": "status", "level": "warn", "message": str(exc), "topic": topic})

    def subscribe(self):
        self.write({"type": "status", "level": "info", "message": "ROS image stream connected", "topics": self.topics})
        for topic in self.topics:
            rospy.Subscriber(topic, Image, lambda msg, t=topic: self.image_cb(t, msg), queue_size=1)

    def heartbeat(self):
        now = time.time()
        if self.last_image <= 0 and now - self.started > 3.0 and now - self.last_warn > 3.0:
            self.last_warn = now
            self.write({"type": "status", "level": "warn", "message": "图像 topic 暂无帧，请确认相机已启动并在发布 /left_camera/image 或 /rgb_img"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--topics", default="/rgb_img,/left_camera/image")
    parser.add_argument("--hz", type=float, default=5.0)
    parser.add_argument("--width", type=int, default=560)
    parser.add_argument("--quality", type=int, default=72)
    args = parser.parse_args()

    topics = [topic.strip() for topic in args.topics.split(",") if topic.strip()]
    rospy.init_node("fast_livo2_console_image_stream", anonymous=True, disable_signals=True)
    streamer = ImageStreamer(topics, args.hz, args.width, args.quality)
    streamer.subscribe()
    rate = rospy.Rate(2)
    while not rospy.is_shutdown():
        streamer.heartbeat()
        rate.sleep()


if __name__ == "__main__":
    main()
