#!/usr/bin/env python3
"""激光雷达探针：频率、点数、角度范围、扇区完整性。

注意：LakiBeam1 物理视场约 ±120~135°，后方为 inf/nan 属正常，
所以这里同时输出「有效点数」和「各扇区有效点数」，由判定层决定怎么用。
"""
import json
import math
import sys

import rospy
from sensor_msgs.msg import LaserScan


def main():
    topic = sys.argv[1] if len(sys.argv) > 1 else "/tianracer/scan"
    frames = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    rospy.init_node("tt_lidar_probe", anonymous=True, disable_signals=True)
    try:
        first = rospy.wait_for_message(topic, LaserScan, timeout=12)
    except rospy.ROSException:
        print("RESULT " + json.dumps({"ok": False, "reason": "no_message"}))
        return

    msgs = [first]
    for _ in range(frames - 1):
        try:
            msgs.append(rospy.wait_for_message(topic, LaserScan, timeout=6))
        except rospy.ROSException:
            break

    m = msgs[-1]
    n = len(m.ranges)

    def valid(values):
        return [v for v in values
                if v == v and m.range_min < v < m.range_max]

    def sector(a0, a1):
        picked = []
        for i in range(n):
            angle = math.degrees(m.angle_min + i * m.angle_increment)
            if a0 <= angle <= a1:
                r = m.ranges[i]
                if r == r and m.range_min < r < m.range_max:
                    picked.append(r)
        return len(picked)

    valid_all = valid(list(m.ranges))
    hz = None
    if len(msgs) >= 2:
        span = (msgs[-1].header.stamp - msgs[0].header.stamp).to_sec()
        if span > 0:
            hz = round((len(msgs) - 1) / span, 3)

    print("RESULT " + json.dumps({
        "ok": True,
        "frames": len(msgs),
        "hz": hz,
        "points": n,
        "valid_points": len(valid_all),
        "valid_ratio": round(len(valid_all) / n, 3) if n else 0,
        "angle_min_deg": round(math.degrees(m.angle_min), 2),
        "angle_max_deg": round(math.degrees(m.angle_max), 2),
        "angle_increment_deg": round(math.degrees(m.angle_increment), 4),
        "range_min": m.range_min,
        "range_max": m.range_max,
        "observed_max_range": round(max(valid_all), 2) if valid_all else None,
        "sectors": {
            "front": sector(-30, 30),
            "rear": sector(150, 210),
            "left": sector(60, 120),
            "right": sector(-120, -60),
        },
        "frame_id": m.header.frame_id,
    }))


if __name__ == "__main__":
    main()
