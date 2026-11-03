#!/usr/bin/env python3
"""IMU 探针：采集消息，统计频率与静止姿态。

方法学注意（与 odom_probe 同）：用 Subscriber 回调计数，
不要用 wait_for_message 循环——后者每次重建订阅，会把 50 Hz 测成几 Hz。

在车上执行，输出一行 `RESULT {json}` 供解析。
"""
import json
import math
import sys
import time

import rospy
from sensor_msgs.msg import Imu

# 默认话题，仅供手工调试；正式调用由上位机传参
DEFAULT_TOPIC = "/tianracer/imu_raw"

_count = 0
_samples = []
_first = None


def _callback(msg):
    global _count, _first
    if _first is None:
        _first = msg
    _count += 1
    if len(_samples) < 40:          # 只留够算均值的量
        _samples.append(msg)


def main():
    topic = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TOPIC
    window = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0

    rospy.init_node("tt_imu_probe", anonymous=True, disable_signals=True)
    sub = rospy.Subscriber(topic, Imu, _callback, queue_size=200)  # noqa: F841

    deadline = time.time() + 12
    while _first is None and time.time() < deadline and not rospy.is_shutdown():
        rospy.sleep(0.05)
    if _first is None:
        print("RESULT " + json.dumps({"ok": False, "reason": "no_message"}))
        return

    t_start = rospy.Time.now()
    count_start = _count
    rospy.sleep(window)
    elapsed = (rospy.Time.now() - t_start).to_sec()
    measured = _count - count_start
    hz = round(measured / elapsed, 2) if elapsed > 0 else None

    msgs = _samples or [_first]
    ax = sum(m.linear_acceleration.x for m in msgs) / len(msgs)
    ay = sum(m.linear_acceleration.y for m in msgs) / len(msgs)
    az = sum(m.linear_acceleration.z for m in msgs) / len(msgs)
    gx = sum(m.angular_velocity.x for m in msgs) / len(msgs)
    gy = sum(m.angular_velocity.y for m in msgs) / len(msgs)
    gz = sum(m.angular_velocity.z for m in msgs) / len(msgs)

    q = msgs[-1].orientation
    roll = math.atan2(2 * (q.w * q.x + q.y * q.z), 1 - 2 * (q.x * q.x + q.y * q.y))
    pitch = math.asin(max(-1.0, min(1.0, 2 * (q.w * q.y - q.z * q.x))))

    print("RESULT " + json.dumps({
        "ok": True,
        "samples": len(msgs),
        "hz": hz,
        "window_sec": round(elapsed, 2),
        "accel": {"x": round(ax, 4), "y": round(ay, 4), "z": round(az, 4)},
        "accel_norm": round(math.sqrt(ax * ax + ay * ay + az * az), 4),
        "gyro": {"x": round(gx, 5), "y": round(gy, 5), "z": round(gz, 5)},
        "roll_deg": round(math.degrees(roll), 2),
        "pitch_deg": round(math.degrees(pitch), 2),
        "frame_id": msgs[-1].header.frame_id,
    }))


if __name__ == "__main__":
    main()
