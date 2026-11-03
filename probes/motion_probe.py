#!/usr/bin/env python3
"""运动探针：发一段定速定时的 ackermann 指令，测里程计反应。

用法：
    motion_probe.py <speed> <steer> <duration> <label> [max_speed] [max_duration]
                    [cmd_topic] [odom_topic]

话题由上位机按产品配置传入，探针里不写死任何产品名。

安全设计（三重保护）：
1. 本脚本内部硬性拒绝超过 max_speed / max_duration 的参数
2. 无论怎么退出，finally 里一定发一次零速指令
3. 结束时立即回零并确认

输出一行 `RESULT {json}`。
"""
import json
import math
import sys

import rospy
from ackermann_msgs.msg import AckermannDrive
from nav_msgs.msg import Odometry

# 默认值只在手工调试时用到；正式调用一律由上位机传参
DEFAULT_CMD_TOPIC = "/tianracer/ackermann_cmd"
DEFAULT_ODOM_TOPIC = "/tianracer/odom"

# 硬编码上限。命令行传进来的值只能更严格，不能更宽松。
# 安全上限不能由被保护对象自己提供（审查发现：
# 之前 baseline.yaml 改成 5 m/s × 60 s 探针就会照做）。
HARD_MAX_SPEED = 0.5      # m/s
HARD_MAX_DURATION = 10.0  # s
HARD_MAX_STEER = 0.6      # rad，约 34°


def yaw_of(q):
    return math.atan2(2 * (q.w * q.z + q.x * q.y),
                      1 - 2 * (q.y * q.y + q.z * q.z))


def publish(pub, speed, steer, times=6):
    msg = AckermannDrive()
    msg.speed = float(speed)
    msg.steering_angle = float(steer)
    for _ in range(times):
        pub.publish(msg)
        rospy.sleep(0.05)


def main():
    speed = float(sys.argv[1]) if len(sys.argv) > 1 else 0.05
    steer = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else 0.3
    label = sys.argv[4] if len(sys.argv) > 4 else "pulse"
    max_speed = float(sys.argv[5]) if len(sys.argv) > 5 else HARD_MAX_SPEED
    max_duration = float(sys.argv[6]) if len(sys.argv) > 6 else HARD_MAX_DURATION
    cmd_topic = sys.argv[7] if len(sys.argv) > 7 else DEFAULT_CMD_TOPIC
    odom_topic = sys.argv[8] if len(sys.argv) > 8 else DEFAULT_ODOM_TOPIC

    # 双重保险：配置值只能收紧，不能放宽硬上限
    max_speed = min(max_speed, HARD_MAX_SPEED)
    max_duration = min(max_duration, HARD_MAX_DURATION)

    if abs(speed) > max_speed or duration > max_duration or duration <= 0:
        print("RESULT " + json.dumps({
            "ok": False, "reason": "safety_limit",
            "detail": f"speed={speed} dur={duration} 超出上限 "
                      f"({max_speed} m/s, {max_duration}s)",
        }))
        return

    if abs(steer) > HARD_MAX_STEER:
        print("RESULT " + json.dumps({
            "ok": False, "reason": "safety_limit",
            "detail": f"steer={steer} rad 超出上限 {HARD_MAX_STEER}",
        }))
        return

    rospy.init_node("tt_motion_probe", anonymous=True, disable_signals=True)
    pub = rospy.Publisher(cmd_topic, AckermannDrive, queue_size=10)

    # 等订阅者连上，否则消息直接丢掉
    deadline = rospy.Time.now().to_sec() + 10
    while pub.get_num_connections() == 0 and rospy.Time.now().to_sec() < deadline:
        rospy.sleep(0.2)
    if pub.get_num_connections() == 0:
        print("RESULT " + json.dumps({"ok": False, "reason": "no_subscriber"}))
        return

    try:
        start = rospy.wait_for_message(odom_topic, Odometry, timeout=6)
        p0 = start.pose.pose.position
        y0 = yaw_of(start.pose.pose.orientation)

        cmd = AckermannDrive()
        cmd.speed = speed
        cmd.steering_angle = steer
        rate = rospy.Rate(10)
        t0 = rospy.Time.now().to_sec()
        while rospy.Time.now().to_sec() - t0 < duration and not rospy.is_shutdown():
            pub.publish(cmd)
            rate.sleep()

        end = rospy.wait_for_message(odom_topic, Odometry, timeout=6)
        p1 = end.pose.pose.position
        y1 = yaw_of(end.pose.pose.orientation)

        dx = p1.x - p0.x
        dy = p1.y - p0.y
        dist = math.hypot(dx, dy)
        dyaw = math.degrees((y1 - y0 + math.pi) % (2 * math.pi) - math.pi)

        print("RESULT " + json.dumps({
            "ok": True,
            "label": label,
            "speed_cmd": speed,
            "steer_cmd": steer,
            "duration": duration,
            "dx": round(dx, 6),
            "dy": round(dy, 6),
            "dist_m": round(dist, 6),
            "yaw0_deg": round(math.degrees(y0), 3),
            "yaw1_deg": round(math.degrees(y1), 3),
            "dyaw_deg": round(dyaw, 3),
            "avg_speed": round(dist / duration, 4) if duration > 0 else None,
        }))
    finally:
        # 无论如何都要回零，避免车继续跑
        try:
            publish(pub, 0.0, 0.0)
        except Exception:
            pass


if __name__ == "__main__":
    main()
