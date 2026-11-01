#!/usr/bin/env python3
"""激光雷达探针：频率、点数、角度范围、扇区完整性。

**频率必须用回调计数，不能用 wait_for_message 循环 + 首末时间戳。**

实车踩过的坑：旧实现只采 5 帧、用 (n-1)/时间跨度 算频率。
5 帧在 30 Hz 下跨度只有 0.167 秒，中间多等 0.04 秒，
算出来就是 24 Hz —— 把一台正常的车判成致命失败。

现在改成：Subscriber 回调计数 + 固定时间窗口，
与 odom_probe / imu_probe 保持一致。

注意：LakiBeam1 物理视场约 ±120~135°，后方为 inf/nan 属正常，
所以这里同时输出「有效点数」和「各扇区有效点数」，由判定层决定怎么用。
"""
import json
import math
import sys
import time

import rospy
from sensor_msgs.msg import LaserScan

_count = 0
_first = None
_last = None


def _callback(msg):
    global _count, _first, _last
    if _first is None:
        _first = msg
    _last = msg
    _count += 1


def main():
    topic = sys.argv[1] if len(sys.argv) > 1 else "/tianracer/scan"
    window = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0

    rospy.init_node("tt_lidar_probe", anonymous=True, disable_signals=True)
    sub = rospy.Subscriber(topic, LaserScan, _callback, queue_size=50)  # noqa: F841

    # 等第一帧，确认话题活着
    deadline = time.time() + 12
    while _first is None and time.time() < deadline and not rospy.is_shutdown():
        rospy.sleep(0.05)
    if _first is None:
        print("RESULT " + json.dumps({"ok": False, "reason": "no_message"}))
        return

    # 固定时间窗口统计帧数
    t_start = rospy.Time.now()
    count_start = _count
    rospy.sleep(window)
    elapsed = (rospy.Time.now() - t_start).to_sec()
    measured = _count - count_start
    hz = round(measured / elapsed, 3) if elapsed > 0 else None

    m = _last
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

    print("RESULT " + json.dumps({
        "ok": True,
        "frames": measured,
        "window_sec": round(elapsed, 2),
        "total_received": _count,
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
