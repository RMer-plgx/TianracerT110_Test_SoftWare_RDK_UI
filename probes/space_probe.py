#!/usr/bin/env python3
"""用激光雷达判断：以某个转角转某段弧，车会不会碰到东西。

为什么需要
----------
2026-11-10 第一次转向标定撞了车。根因不是「场地小」，而是**没有先测就开跑**：
按角度升序跑，而角度越小半径越大（R = L/tan δ），
0.10 rad 需要 5 米见方，实测右侧只有 1.0 米。

事后看，雷达一直在扫，只是没人问它。这个探针把「够不够」从估计变成测量。

怎么算才准
----------
车不是质点。阿克曼转弯时回转中心在车身侧面，车体绕它画出一个**圆环**
（内半径 = 最近车身点到中心，外半径 = 最远角点）。

简化成「半径 R 的圆」会低估 25% 左右 —— 外侧前角扫得比轴心远。
本探针按圆环算，并把雷达点重新投影到回转中心坐标系里逐点判定。

用法
----
    space_probe.py [scan_topic] [samples] [steer_rad] [speed] [seconds]

给了 steer/speed/seconds 就额外判定这一次动作是否安全：

    sweep.verdict      "safe" / "unsafe" / "unknown"
    sweep.worst_m      扫掠区内障碍到环边缘的最小裕量（负数=压线）
    sweep.annulus      内/外半径与扫过角度

不给就只报告空间本身（clearance_m / max_circle_m）。
"""

from __future__ import annotations

import json
import math
import sys

DEFAULT_SCAN_TOPIC = "/tianracer/scan"

# ---- 车体几何（T110）。转圈判定用，宁可保守 ----
WHEELBASE = 0.255        # 轴距，2026-11-10 实测
HALF_WIDTH = 0.115       # 半车宽
FRONT_OVERHANG = 0.16    # 前轴到车头
REAR_OVERHANG = 0.10     # 后轴到车尾
SAFETY_MARGIN = 0.15     # 判定时额外留的余量

# 打滑导致的**终点位置漂移**（米），仅作提示，不进扫掠判定。
#
# 2026-11-10 实测：转满一圈后车偏离起点 1.12 m（左）/ 1.27 m（右）。
# 但同一测试在最近障碍 1.48 m 的场地完成且未碰撞 —— 说明漂移发生在
# 扫掠范围之内（打滑让轨迹内缩成螺旋，不向外扩）。
#
# 所以**不能**把漂移量直接加到扫掠外环上：那会把 2.5 m 的场地判成
# unsafe，而实测 1.5 m 就能转。仅作为报告提示，提醒操作者留出返程空间。
SLIP_DRIFT_NOTE_M = 1.30

# 扫掠判定是否加打滑余量。默认 False —— 几何扫掠才是碰撞判据。
# 若某天发现轨迹确实外扩（例如打滑方向相反），再打开并重新标定数值。
SLIP_INFLATE = False

FAR_M = 30.0

SECTORS = [
    ("front", -22.5, 22.5),
    ("front_left", 22.5, 67.5),
    ("left", 67.5, 112.5),
    ("rear_left", 112.5, 157.5),
    ("rear", 157.5, 180.0),
    ("rear", -180.0, -157.5),
    ("rear_right", -157.5, -112.5),
    ("right", -112.5, -67.5),
    ("front_right", -67.5, -22.5),
]


def valid_range(value) -> bool:
    return value is not None and math.isfinite(value) and 0.0 < value < FAR_M


def sector_min(scan, lo_deg, hi_deg):
    best = None
    angle = scan.angle_min
    for r in scan.ranges:
        deg = math.degrees(angle)
        in_sector = (lo_deg <= deg <= hi_deg) if lo_deg <= hi_deg else \
                    (deg >= lo_deg or deg <= hi_deg)
        if in_sector and valid_range(r):
            best = r if best is None else min(best, r)
        angle += scan.angle_increment
    return best


def car_points():
    """车体矩形的四个角（后轴为原点，x 向前，y 向左）。"""
    return [
        (FRONT_OVERHANG + WHEELBASE, HALF_WIDTH),
        (FRONT_OVERHANG + WHEELBASE, -HALF_WIDTH),
        (-REAR_OVERHANG, HALF_WIDTH),
        (-REAR_OVERHANG, -HALF_WIDTH),
    ]


def annulus(turn_radius: float):
    """绕回转中心转整圈时，车体扫过的圆环内/外半径。"""
    inner = abs(turn_radius - HALF_WIDTH)
    outer = 0.0
    for x, y in car_points():
        d = math.hypot(x, y - turn_radius)
        outer = max(outer, d)
    return inner, outer


def swept_check(scan, steer_rad, speed, seconds, wheelbase=WHEELBASE):
    """判定按给定参数跑 seconds 秒是否会碰。

    把每个雷达点从「车体坐标系」变换到「回转中心坐标系」，
    看它是否落在「扫过的扇形环」里。落入即视为会碰。
    """
    if abs(steer_rad) < 1e-3:
        return {"verdict": "unknown", "reason": "转角为 0，是直线"}

    turn_radius = wheelbase / math.tan(abs(steer_rad))
    inner, outer = annulus(turn_radius)

    omega = abs(speed) / turn_radius                    # rad/s
    sweep_deg = math.degrees(omega * seconds)           # 扫过角度

    # 默认不加打滑余量：实测表明漂移在扫掠范围之内，不是向外扩。
    # 打开 SLIP_INFLATE 会把保守度提高，但也可能把可行场地误判为不可行。
    slip_allow = (SLIP_DRIFT_NOTE_M * min(1.0, sweep_deg / 360.0)
                  if SLIP_INFLATE else 0.0)

    sign = 1.0 if steer_rad > 0 else -1.0
    cy = sign * turn_radius                             # 回转中心（车体坐标）

    # ---- 区分两类危险 ----
    # 之前的 `worst_m` 把「真的落在环内（会撞）」与「环外但余量不够」
    # 混成一个数，现场看不出差在哪。现在分开算：
    #   conflict_m  落在 [inner, outer] 内的最近点 → 负值表示车内会扫到
    #   margin_m    环外障碍到环边缘的距离，越小越紧（仅作参考）
    conflict = None      # 最近的点离环中线的距离，负数=在环内
    margin = None        # 环外最近障碍到环边缘的距离
    hits = 0
    angle = scan.angle_min
    for r in scan.ranges:
        deg = math.degrees(angle)
        angle += scan.angle_increment
        if not valid_range(r):
            continue
        # 雷达点（车体坐标；忽略雷达自身 0.175 m 前移，偏保守）
        px = r * math.cos(math.radians(deg))
        py = r * math.sin(math.radians(deg))
        vx, vy = px, py - cy
        dist = math.hypot(vx, vy)
        point_ang = math.degrees(math.atan2(vy, vx))
        if sign > 0:                     # 左转：绕中心逆时针
            start = -90.0
            delta = (point_ang - start) % 360.0
        else:                            # 右转：顺时针
            start = 90.0
            delta = (start - point_ang) % 360.0
        if delta > sweep_deg + 5:        # 5° 余量：弧外的不算
            continue

        outer_with_slip = outer + slip_allow
        if inner <= dist <= outer_with_slip:
            # 在扫掠环内（含打滑漂移余量）—— 车会碰上
            hits += 1
            near = min(dist - inner, outer_with_slip - dist)
            conflict = near if conflict is None else min(conflict, near)
        else:
            gap = min(abs(dist - inner), abs(dist - outer_with_slip))
            margin = gap if margin is None else min(margin, gap)

    verdict = "unsafe" if hits else "safe"
    return {
        "verdict": verdict,
        "conflict_m": round(conflict, 3) if conflict is not None else None,
        "margin_m": round(margin, 3) if margin is not None else None,
        "points_in_sweep": hits,
        "annulus": {
            "inner_m": round(inner, 3),
            "outer_m": round(outer, 3),
            "outer_with_slip_m": round(outer + slip_allow, 3),
            "slip_allowance_m": round(slip_allow, 3),
            "turn_radius_m": round(turn_radius, 3),
            "sweep_deg": round(sweep_deg, 1),
        },
        # 提示项：整圈跑完车会偏离起点多少（实测值，非判定依据）
        "slip_drift_note_m": (round(SLIP_DRIFT_NOTE_M, 2)
                              if abs(sweep_deg) >= 300 else None),
    }


def analyse(scan, steer=None, speed=None, seconds=None):
    sectors = {}
    for name, lo, hi in SECTORS:
        d = sector_min(scan, lo, hi)
        if name in sectors and sectors[name] is not None and d is not None:
            sectors[name] = min(sectors[name], d)
        elif name not in sectors or sectors[name] is None:
            sectors[name] = d

    rear_names = [n for n, _, _ in SECTORS if n.startswith("rear")]
    rear_blind = any(sectors.get(n) is None for n in rear_names)
    observed = [d for d in sectors.values() if d is not None]
    clearance = min(observed) if observed else None
    max_circle = max(0.0, clearance - HALF_WIDTH) if clearance else None

    out = {
        "clearance_m": round(clearance, 3) if clearance is not None else None,
        "max_circle_m": round(max_circle, 3) if max_circle is not None else None,
        "rear_blind": rear_blind,
        "sectors": {k: (round(v, 3) if v is not None else None)
                    for k, v in sectors.items()},
        "geometry": {
            "wheelbase_m": WHEELBASE,
            "half_width_m": HALF_WIDTH,
            "front_overhang_m": FRONT_OVERHANG,
            "rear_overhang_m": REAR_OVERHANG,
            "safety_margin_m": SAFETY_MARGIN,
        },
    }
    if None not in (steer, speed, seconds):
        out["sweep"] = swept_check(scan, steer, speed, seconds)
    return out


def main():
    try:
        import rospy
        from sensor_msgs.msg import LaserScan
    except Exception as exc:
        print("RESULT " + json.dumps({"ok": False, "reason": "no_ros",
                                      "detail": str(exc)}))
        return

    topic = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SCAN_TOPIC
    samples = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    steer = float(sys.argv[3]) if len(sys.argv) > 3 else None
    speed = float(sys.argv[4]) if len(sys.argv) > 4 else None
    seconds = float(sys.argv[5]) if len(sys.argv) > 5 else None

    rospy.init_node("tt_space_probe", anonymous=True, disable_signals=True)

    worst = None
    frames = [0]

    def on_scan(msg):
        nonlocal worst
        info = analyse(msg, steer, speed, seconds)
        if worst is None:
            worst = info
        else:
            if info["clearance_m"] is not None and (
                    worst["clearance_m"] is None
                    or info["clearance_m"] < worst["clearance_m"]):
                worst["clearance_m"] = info["clearance_m"]
                worst["max_circle_m"] = info["max_circle_m"]
                worst["sectors"] = info["sectors"]
            if "sweep" in info:
                prev = worst.get("sweep") or {}
                cur = info["sweep"]
                if prev.get("verdict") == "unsafe" or cur.get("verdict") == "unsafe":
                    worst["sweep"] = cur if cur.get("verdict") == "unsafe" else prev
                elif prev.get("margin_m") is None or (
                        cur.get("margin_m") is not None
                        and cur["margin_m"] < prev["margin_m"]):
                    worst["sweep"] = cur
        frames[0] += 1

    rospy.Subscriber(topic, LaserScan, on_scan, queue_size=5)

    deadline = rospy.Time.now().to_sec() + 10
    while rospy.Time.now().to_sec() < deadline and frames[0] < samples:
        if rospy.is_shutdown():
            break
        rospy.sleep(0.1)

    if worst is None:
        print("RESULT " + json.dumps({"ok": False, "reason": "no_scan",
                                      "detail": f"10 秒内没收到 {topic}"}))
        return

    worst.update({"ok": True, "frames": frames[0], "topic": topic})
    print("RESULT " + json.dumps(worst, ensure_ascii=False))


if __name__ == "__main__":
    main()
