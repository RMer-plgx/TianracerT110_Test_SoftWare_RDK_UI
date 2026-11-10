#!/usr/bin/env python3
"""转向互校探针：闭环转一整圈，同时记录 odom 与 IMU 各自的航向累积。

用途
----
架空段只能验证「IMU / 里程计有数据、频率对、静止时不漂」，
证明不了**走起来以后数据准不准**。落地转一整圈才能：
  · 比对里程计推算的航向与陀螺仪测得的航向是否一致
  · 检查转完一圈是否回到起点附近（闭合误差）

方向控制用 **IMU**（直接测量，不打滑），里程计只作对照。
这样两者才是独立来源，互校才有意义。

用法
----
    spin_probe.py <speed> <steer> <target_yaw_deg> <max_seconds> <label>
                  [cmd_topic] [odom_topic] [imu_topic]

话题由上位机按产品配置传入，探针里不写死任何产品名。

`steer` 的符号决定方向：正数左转（yaw 增大），负数右转。

安全设计（四重）
----------------
1. 脚本内硬性拒绝超限参数，命令行值只能更严格
2. 每圈检查 /tmp/tt_spin_abort，存在即立即停车退出
   —— 用户点「停止」时由上位机创建该文件。这是唯一能打断
      正在旋转的车的手段：另开连接发零速会被本进程 20 Hz 覆盖。
3. |yaw| 超出目标 30% 立即判定异常并停车
4. finally 里无论如何都发零速

输出一行 `RESULT {json}`。
"""
import json
import math
import os
import sys

import rospy
from ackermann_msgs.msg import AckermannDrive
from nav_msgs.msg import Odometry
from sensor_msgs.msg import Imu

# 默认值只在手工调试时用到；正式调用一律由上位机传参
DEFAULT_CMD_TOPIC = "/tianracer/ackermann_cmd"
DEFAULT_ODOM_TOPIC = "/tianracer/odom"
DEFAULT_IMU_TOPIC = "/tianracer/imu_raw"
ABORT_FILE = "/tmp/tt_spin_abort"

# 硬编码上限：转圈比直线危险得多，必须由脚本自己守死
HARD_MAX_SPEED = 0.25      # m/s，转圈时轮胎侧向受力大，普通测试用不到这么快
HARD_MAX_STEER = 0.5       # rad，约 29°
HARD_MAX_SECONDS = 120.0   # 单方向最长 2 分钟
HARD_MAX_TARGET = 400.0    # 允许略超一圈，但不允许无限转


def wrap(angle: float) -> float:
    """把角度归一到 [-pi, pi]。"""
    return (angle + math.pi) % (2 * math.pi) - math.pi


def quat_to_yaw(x: float, y: float, z: float, w: float) -> float:
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


def covered_deg(total_rad: float, base_rad: float) -> float:
    """已转过的角度，**单位：度**。

    `YawTracker.total` 是弧度（`quat_to_yaw` 用 atan2，再累加 wrap 的弧度差），
    而目标角度与异常阈值来自命令行参数，是度。两者直接比会永远不成立：
    转满 360° 只累积 6.28，而阈值写着 360。

    曾经就是这样：探针一直不判 reached，车转到超时才停。
    实车表现为左右各转 91 秒、累积 600~900°、闭合误差 1.4 m。

    判定（`travelled >= stop_at`）与上报（`imu_yaw_deg`）都走这个函数，
    单位只在这里定一次。
    """
    return math.degrees(abs(total_rad - base_rad))


class YawTracker:
    """累积航向变化，正确处理 ±180° 跨越和超过一圈的情况。

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

    @property
    def degrees(self) -> float:
        return math.degrees(self.total)


def publish(pub, speed, steer, times=6):
    msg = AckermannDrive()
    msg.speed = float(speed)
    msg.steering_angle = float(steer)
    for _ in range(times):
        pub.publish(msg)
        rospy.sleep(0.02)


def main():
    speed = float(sys.argv[1]) if len(sys.argv) > 1 else 0.12
    steer = float(sys.argv[2]) if len(sys.argv) > 2 else 0.35
    target = float(sys.argv[3]) if len(sys.argv) > 3 else 360.0
    max_seconds = float(sys.argv[4]) if len(sys.argv) > 4 else 90.0
    label = sys.argv[5] if len(sys.argv) > 5 else "spin"
    cmd_topic = sys.argv[6] if len(sys.argv) > 6 else DEFAULT_CMD_TOPIC
    odom_topic = sys.argv[7] if len(sys.argv) > 7 else DEFAULT_ODOM_TOPIC
    imu_topic = sys.argv[8] if len(sys.argv) > 8 else DEFAULT_IMU_TOPIC

    # 参数只能收紧，不能放宽硬上限
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
    if abs(target) > HARD_MAX_TARGET:
        print("RESULT " + json.dumps({
            "ok": False, "reason": "safety_limit",
            "detail": f"target={target} 超过硬上限 {HARD_MAX_TARGET}°"}))
        return
    if max_seconds <= 0 or max_seconds > HARD_MAX_SECONDS:
        print("RESULT " + json.dumps({
            "ok": False, "reason": "safety_limit",
            "detail": f"max_seconds={max_seconds} 超过硬上限 {HARD_MAX_SECONDS}s"}))
        return

    # 清掉上一次可能残留的中止标志，避免一启动就退出
    try:
        os.remove(ABORT_FILE)
    except OSError:
        pass

    rospy.init_node("tt_spin_probe", anonymous=True, disable_signals=True)
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
    imu_gyro = {"z": 0.0, "samples": 0}
    imu_has_orientation = True
    last_imu_yaw = None

    def on_odom(msg):
        nonlocal start_pos, end_pos
        p = msg.pose.pose.position
        if start_pos["x"] is None:
            start_pos["x"], start_pos["y"] = p.x, p.y
        end_pos["x"], end_pos["y"] = p.x, p.y
        q = msg.pose.pose.orientation
        odom_tracker.update(quat_to_yaw(q.x, q.y, q.z, q.w))

    def on_imu(msg):
        nonlocal imu_has_orientation, last_imu_yaw
        q = msg.orientation
        # orientation 无效时（协方差首元素为 -1），改用角速度积分
        if q.w == 0.0 and q.x == 0.0 and q.y == 0.0 and q.z == 0.0:
            imu_has_orientation = False
            return
        yaw = quat_to_yaw(q.x, q.y, q.z, q.w)
        last_imu_yaw = yaw
        imu_tracker.update(yaw)
        imu_gyro["z"] += msg.angular_velocity.z
        imu_gyro["samples"] += 1

    rospy.Subscriber(odom_topic, Odometry, on_odom, queue_size=50)
    rospy.Subscriber(imu_topic, Imu, on_imu, queue_size=50)

    # 等两路数据都到，否则起点位置和初始 yaw 会漏
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

    # 记录起始位置快照（等数据稳定后再取）
    p0 = dict(start_pos)
    odom0 = odom_tracker.total
    imu0 = imu_tracker.total

    sign = 1.0 if target >= 0 else -1.0
    stop_at = abs(target)
    anomaly_at = stop_at * 1.3

    cmd = AckermannDrive()
    cmd.speed = speed
    cmd.steering_angle = steer
    rate = rospy.Rate(20)
    t0 = rospy.Time.now().to_sec()
    aborted = False
    anomaly = ""
    reached = False

    gyro_accum = 0.0
    try:
        while not rospy.is_shutdown():
            elapsed = rospy.Time.now().to_sec() - t0
            if elapsed > max_seconds:
                anomaly = f"超时 {max_seconds:.0f}s 未达到 {stop_at:.0f}°"
                break

            # 用户点停止：上位机会创建这个文件
            if os.path.exists(ABORT_FILE):
                aborted = True
                break

            # 单位统一由 covered_deg() 负责（弧度 → 度）。
            # `stop_at` / `anomaly_at` 是度，这里必须是度才能比较。
            travelled = covered_deg(imu_tracker.total, imu0)
            if travelled >= anomaly_at:
                anomaly = (f"已转过 {travelled:.0f}°，"
                           f"超出目标 {stop_at:.0f}° 的 30%，判定异常")
                break
            if travelled >= stop_at:
                reached = True
                break

            pub.publish(cmd)
            rate.sleep()
    finally:
        try:
            publish(pub, 0.0, 0.0)
        except Exception:
            pass

    # 停车后再采一小段，让最后一帧数据落地
    rospy.sleep(1.2)

    imu_total = math.degrees(imu_tracker.total - imu0)
    odom_total = math.degrees(odom_tracker.total - odom0)
    duration = rospy.Time.now().to_sec() - t0

    if not imu_has_orientation and imu_gyro["samples"] > 0:
        # 用角速度积分兜底（乘上平均采样间隔的近似）
        imu_total = math.degrees(
            imu_gyro["z"] / imu_gyro["samples"] * duration)

    closure = None
    if None not in (p0.get("x"), p0.get("y"), end_pos.get("x"), end_pos.get("y")):
        closure = math.hypot(end_pos["x"] - p0["x"], end_pos["y"] - p0["y"])

    print("RESULT " + json.dumps({
        "ok": True,
        "label": label,
        "aborted": aborted,
        "reached": reached,
        "anomaly": anomaly,
        "target_deg": target,
        "imu_yaw_deg": round(imu_total, 2),
        "odom_yaw_deg": round(odom_total, 2),
        "yaw_diff_deg": round(abs(imu_total - odom_total), 2),
        "closure_m": round(closure, 4) if closure is not None else None,
        "duration": round(duration, 2),
        "samples": odom_tracker.samples,
        "imu_source": "orientation" if imu_has_orientation else "gyro_z",
        "speed_cmd": speed,
        "steer_cmd": steer,
    }))


if __name__ == "__main__":
    main()
