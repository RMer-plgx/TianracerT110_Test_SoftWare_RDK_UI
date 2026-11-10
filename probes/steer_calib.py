#!/usr/bin/env python3
"""转向标定探针 —— 恒定转角跑固定时长，测出车实际转了多少。

为什么要单独一个探针
--------------------
`spin_probe.py` 是为「转够一圈就停」设计的，判据是 `reached`。
标定要的是另一件事：**给定转角，测稳态角速度**，不需要 target 判定，
也不想被 `target` 的硬上限约束。混在一起会让两边的语义都变模糊。

测什么
------
给定 `(speed, steer, duration)`，发恒定指令，记录：

    imu_yaw_deg    车体实际转过的角度（IMU 逐样本累积，跨 ±180° 折叠）
    odom_yaw_deg   里程计推算的航向（来自轮速，打滑时会偏大）
    chord_m        起终点直线距离（理想圆弧上 = 2R·sin(θ/2)）
    duration       实际执行时长（以 ROS 时间为准，非累加 sleep）

上游怎么用
----------
`tools/steer_calib.py` 拿这些数据反推真实转向角：

    ω = imu_yaw_deg / duration
    打滑率 s = (odom_yaw_deg - imu_yaw_deg) / imu_yaw_deg
    真实速度 v ≈ v_cmd / (1 + s)
    R = v / ω,  δ_real = atan(L / R)

这是不改 MCU 固件（无角度回传）时唯一的间接测量手段。

安全
----
- 参数有硬上限，超过直接拒绝执行（与 spin_probe 同一套数值）
- 固定时长到点即停，发零速
- 每轮检查 `/tmp/tt_spin_abort`，上位机可随时中止
- 100 Hz 采样、20 Hz 发指令

用法
----
    steer_calib.py <speed> <steer> <duration> <label>
                   [cmd_topic] [odom_topic] [imu_topic]

输出一行 `RESULT {json}`。
"""

from __future__ import annotations

import json
import math
import os
import sys

# ---- 硬上限：与 spin_probe 保持一致，脚本自己守死，不信上游 ----
HARD_MAX_SPEED = 0.25      # m/s
HARD_MAX_STEER = 0.5       # rad
HARD_MAX_DURATION = 30.0   # s，标定不需要跑很久

ABORT_FILE = "/tmp/tt_spin_abort"

DEFAULT_CMD_TOPIC = "/tianracer/ackermann_cmd"
DEFAULT_ODOM_TOPIC = "/tianracer/odom"
DEFAULT_IMU_TOPIC = "/tianracer/imu"

try:
    import rospy
    from ackermann_msgs.msg import AckermannDrive
    from nav_msgs.msg import Odometry
    from sensor_msgs.msg import Imu
except Exception:  # 纯数学部分在无 ROS 环境里仍可被测试导入
    rospy = None


def wrap(angle: float) -> float:
    """把角度归一到 [-pi, pi]。"""
    return (angle + math.pi) % (2 * math.pi) - math.pi


def quat_to_yaw(x: float, y: float, z: float, w: float) -> float:
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


class YawTracker:
    """累积航向变化，正确处理 ±180° 跨越和超过一圈。

    直接用「末值 - 初值」是错的：转一圈回到 0°，差值算出来是 0。
    """

    def __init__(self):
        self.total = 0.0
        self._prev = None
        self.samples = 0

    def update(self, yaw: float) -> float:
        if self._prev is None:
            self._prev = yaw
        else:
            self.total += wrap(yaw - self._prev)
            self._prev = yaw
            self.samples += 1
        return self.total


def publish(pub, speed, steer, times=6):
    msg = AckermannDrive()
    msg.speed = float(speed)
    msg.steering_angle = float(steer)
    for _ in range(times):
        pub.publish(msg)
        rospy.sleep(0.02)


def main():
    rate_hz = 20.0
    speed = float(sys.argv[1]) if len(sys.argv) > 1 else 0.12
    steer = float(sys.argv[2]) if len(sys.argv) > 2 else 0.25
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else 15.0
    label = sys.argv[4] if len(sys.argv) > 4 else "calib"
    cmd_topic = sys.argv[5] if len(sys.argv) > 5 else DEFAULT_CMD_TOPIC
    odom_topic = sys.argv[6] if len(sys.argv) > 6 else DEFAULT_ODOM_TOPIC
    imu_topic = sys.argv[7] if len(sys.argv) > 7 else DEFAULT_IMU_TOPIC

    # ---- 参数硬校验 ----
    if abs(speed) > HARD_MAX_SPEED:
        print("RESULT " + json.dumps({
            "ok": False, "reason": "safety_limit",
            "detail": f"speed={speed} 超过硬上限 {HARD_MAX_SPEED} m/s"}))
        return
    if abs(steer) > HARD_MAX_STEER:
        print("RESULT " + json.dumps({
            "ok": False, "reason": "safety_limit",
            "detail": f"steer={steer} 超过硬上限 {HARD_MAX_STEER} rad"}))
        return
    if duration <= 0 or duration > HARD_MAX_DURATION:
        print("RESULT " + json.dumps({
            "ok": False, "reason": "safety_limit",
            "detail": f"duration={duration} 超过上限 {HARD_MAX_DURATION}s"}))
        return

    try:
        os.remove(ABORT_FILE)
    except OSError:
        pass

    rospy.init_node("tt_steer_calib", anonymous=True, disable_signals=True)
    pub = rospy.Publisher(cmd_topic, AckermannDrive, queue_size=10)

    deadline = rospy.Time.now().to_sec() + 10
    while pub.get_num_connections() == 0 and rospy.Time.now().to_sec() < deadline:
        rospy.sleep(0.2)
    if pub.get_num_connections() == 0:
        print("RESULT " + json.dumps({"ok": False, "reason": "no_subscriber"}))
        return

    odom_tracker = YawTracker()
    imu_tracker = YawTracker()
    start_pos = {"x": None, "y": None}
    end_pos = {"x": None, "y": None}

    def on_odom(msg):
        p = msg.pose.pose.position
        if start_pos["x"] is None:
            start_pos["x"], start_pos["y"] = p.x, p.y
        end_pos["x"], end_pos["y"] = p.x, p.y
        q = msg.pose.pose.orientation
        odom_tracker.update(quat_to_yaw(q.x, q.y, q.z, q.w))

    def on_imu(msg):
        q = msg.orientation
        if q.w == 0.0 and q.x == 0.0 and q.y == 0.0 and q.z == 0.0:
            return
        imu_tracker.update(quat_to_yaw(q.x, q.y, q.z, q.w))

    rospy.Subscriber(odom_topic, Odometry, on_odom, queue_size=50)
    rospy.Subscriber(imu_topic, Imu, on_imu, queue_size=50)

    # 等两路数据都到，否则起点会被漏掉
    wait_until = rospy.Time.now().to_sec() + 8
    while rospy.Time.now().to_sec() < wait_until:
        if odom_tracker.samples > 3 and imu_tracker.samples > 3:
            break
        rospy.sleep(0.1)

    if odom_tracker.samples < 3:
        print("RESULT " + json.dumps({
            "ok": False, "reason": "no_odom",
            "detail": f"8 秒内只收到 {odom_tracker.samples} 帧里程计"}))
        return
    if imu_tracker.samples < 3:
        print("RESULT " + json.dumps({
            "ok": False, "reason": "no_imu",
            "detail": f"8 秒内只收到 {imu_tracker.samples} 帧 IMU"}))
        return

    # 起点快照（等数据稳定后取）
    p0 = dict(start_pos)
    odom0 = odom_tracker.total
    imu0 = imu_tracker.total

    cmd = AckermannDrive()
    cmd.speed = speed
    cmd.steering_angle = steer
    rate = rospy.Rate(rate_hz)
    t0 = rospy.Time.now().to_sec()
    aborted = False

    try:
        while not rospy.is_shutdown():
            if rospy.Time.now().to_sec() - t0 >= duration:
                break
            if os.path.exists(ABORT_FILE):
                aborted = True
                break
            pub.publish(cmd)
            rate.sleep()
    finally:
        try:
            publish(pub, 0.0, 0.0)
        except Exception:
            pass

    actual = rospy.Time.now().to_sec() - t0
    # 停车后再采一小段，让最后一帧落地
    rospy.sleep(1.2)

    imu_deg = math.degrees(imu_tracker.total - imu0)
    odom_deg = math.degrees(odom_tracker.total - odom0)
    chord = None
    if None not in (p0.get("x"), p0.get("y"), end_pos.get("x"), end_pos.get("y")):
        chord = math.hypot(end_pos["x"] - p0["x"], end_pos["y"] - p0["y"])

    print("RESULT " + json.dumps({
        "ok": True,
        "label": label,
        "aborted": aborted,
        "speed_cmd": speed,
        "steer_cmd": steer,
        "duration": round(actual, 3),
        "imu_yaw_deg": round(imu_deg, 3),
        "odom_yaw_deg": round(odom_deg, 3),
        "chord_m": round(chord, 4) if chord is not None else None,
        "imu_samples": imu_tracker.samples,
        "odom_samples": odom_tracker.samples,
    }))


if __name__ == "__main__":
    main()
