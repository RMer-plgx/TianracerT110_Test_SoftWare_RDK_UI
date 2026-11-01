#!/usr/bin/env python3
"""里程计探针：频率与静止漂移。

方法学注意（实车验证踩到）：
不能用 `wait_for_message` 循环来数频率——每次调用都要重新建立订阅并等待，
开销很大，会把 100 Hz 的话题测成 25 Hz。必须用 Subscriber 回调计数。
"""
import json
import math
import sys
import time

import rospy
from nav_msgs.msg import Odometry

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
    topic = sys.argv[1] if len(sys.argv) > 1 else "/tianracer/odom"
    window = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0

    rospy.init_node("tt_odom_probe", anonymous=True, disable_signals=True)
    sub = rospy.Subscriber(topic, Odometry, _callback, queue_size=200)  # noqa: F841

    # 等第一条消息到达，确认话题活着
    deadline = time.time() + 12
    while _first is None and time.time() < deadline and not rospy.is_shutdown():
        rospy.sleep(0.1)
    if _first is None:
        print("RESULT " + json.dumps({"ok": False, "reason": "no_message"}))
        return

    # 实测窗口。用 ros::Time 而非墙钟，避免系统时间跳变干扰
    t_start = rospy.Time.now()
    count_start = _count
    rospy.sleep(window)
    elapsed = (rospy.Time.now() - t_start).to_sec()
    measured = _count - count_start

    hz = round(measured / elapsed, 2) if elapsed > 0 else None

    p0 = _first.pose.pose.position
    p1 = _last.pose.pose.position
    drift = math.hypot(p1.x - p0.x, p1.y - p0.y)

    print("RESULT " + json.dumps({
        "ok": True,
        "hz": hz,
        "window_sec": round(elapsed, 2),
        "messages": measured,
        "total_received": _count,
        "start": {"x": round(p0.x, 6), "y": round(p0.y, 6), "z": round(p0.z, 6)},
        "end": {"x": round(p1.x, 6), "y": round(p1.y, 6), "z": round(p1.z, 6)},
        "static_drift_m": round(drift, 6),
        "twist_linear_x": round(_last.twist.twist.linear.x, 6),
        "twist_angular_z": round(_last.twist.twist.angular.z, 6),
        "frame_id": _last.header.frame_id,
    }))


if __name__ == "__main__":
    main()
