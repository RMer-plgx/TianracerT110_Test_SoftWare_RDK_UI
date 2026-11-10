"""阿克曼转向几何：把舵机角、半径、角速度、时间互相换算。

存在理由
--------
转圈互校原来只做「IMU 与 odom 的航向差」，这只能发现打滑，
**发现不了舵机执行偏差** —— 舵机卡在中位时两个航向都不变，差值是 0，
反而判 PASS。

有了轴距，就能做一件原来做不到的事：从 IMU 实测的角速度反推
**真实舵机角**，与指令对比。这是在不改 MCU 固件（无法回传角度）的前提下
唯一可行的间接测量。

公式（前轮转向、后轴驱动、无侧偏的经典阿克曼模型）：

    R = L / tan(δ)          转弯半径
    ω = v / R               角速度
    θ = ω · T               累积角度

其中 L 是轴距，δ 是前轮转角（rad），v 是后轴中心线速度。

单位约定
--------
本模块**输入输出一律用弧度**（`δ`、`ω`），只有显式带 `_deg` 后缀的
参数或返回值才是度。历史上的单位 bug（弧度当度比）就是靠这个约定防的。
"""

from __future__ import annotations

import math
from typing import Optional


def turning_radius(wheelbase_m: float, steer_rad: float) -> Optional[float]:
    """转弯半径（米）。转向角为 0 时半径无穷大，返回 None。

    `steer_rad` 取绝对值：方向由符号表达，半径只关心大小。
    """
    if wheelbase_m <= 0:
        return None
    delta = abs(steer_rad)
    # 0.001 rad ≈ 0.06°，再小就当成直行
    if delta < 1e-3:
        return None
    return wheelbase_m / math.tan(delta)


def yaw_rate_rad(wheelbase_m: float, steer_rad: float,
                 speed_mps: float) -> Optional[float]:
    """理论角速度（rad/s）。直行或参数非法返回 None。"""
    radius = turning_radius(wheelbase_m, steer_rad)
    if radius is None or radius <= 0:
        return None
    return abs(speed_mps) / radius


def steer_from_radius(wheelbase_m: float, radius_m: float) -> Optional[float]:
    """由半径反推转向角（rad，恒为正）。半径非法返回 None。"""
    if wheelbase_m <= 0 or radius_m is None or radius_m <= 0:
        return None
    return math.atan(wheelbase_m / radius_m)


def steer_from_measurement(wheelbase_m: float, speed_mps: float,
                           yaw_rate_deg_per_s: float) -> Optional[float]:
    """从实测角速度反推真实转向角（rad）。

    ``R = v / ω``，再 ``δ = atan(L / R)``。这是「读不到舵机角度」时的
    唯一间接手段 —— 精度完全取决于 `speed_mps` 是否真实。

    若拿的是指令速度，打滑会让它偏大 → R 偏大 → δ 偏小。
    先过一遍 `correct_for_slip()` 再传进来会准得多。
    """
    omega = abs(math.radians(yaw_rate_deg_per_s))
    if omega < 1e-9:
        return None
    radius = abs(speed_mps) / omega
    return steer_from_radius(wheelbase_m, radius)


def spin_seconds(wheelbase_m: float, steer_rad: float, speed_mps: float,
                 target_deg: float = 360.0) -> Optional[float]:
    """转够 `target_deg` 的理论耗时（秒）。用于给 `max_seconds` 定合理值。"""
    omega = yaw_rate_rad(wheelbase_m, steer_rad, speed_mps)
    if omega is None or omega <= 0:
        return None
    return math.radians(abs(target_deg)) / omega


def slip_ratio(odom_deg: float, imu_deg: float) -> Optional[float]:
    """打滑率：轮子比车多转的比例。

    ``(|θ_odom| − |θ_imu|) / |θ_imu|``

    odom 的累积角来自轮速，IMU 来自陀螺/姿态融合。前者大于后者说明
    轮子在打滑（转了但车没转够）。0.2 表示轮子多转了 20%。

    **必须取绝对值再比。** 右转时两路都是负数，用带符号的差值算：
    ``(-92.9 − (-67.5)) / 67.5 = -0.376`` —— 明明是打滑却算出负值，
    而负打滑会让修正车速变大，反推出一个假的小转角。
    2026-11-10 的标定就是被这个坑了：左转 δ_B=19.0°、右转 δ_B=8.8°，
    看起来像右转舵机坏了一半，实际两边都是 19°。
    负值现在只用于表达「odom 落后于 IMU」（轮子陷地或 IMU 漂移）。
    """
    if imu_deg is None or abs(imu_deg) < 1e-6:
        return None
    if odom_deg is None:
        return None
    return (abs(odom_deg) - abs(imu_deg)) / abs(imu_deg)


def correct_for_slip(speed_cmd_mps: float,
                     slip: Optional[float]) -> float:
    """用打滑率修正车速估计。

    打滑率 s 时，轮速读数是 ``v_real · (1 + s)``，所以
    ``v_real ≈ v_cmd / (1 + s)``。

    一阶近似：假设各轮均匀打滑。若只有单侧轮打滑，这个修正会失真，
    调用方需结合三路交叉验证判断。
    """
    if slip is None or slip <= -0.99:
        return speed_cmd_mps
    return speed_cmd_mps / (1.0 + slip)


def closure_after_turn(radius_m: float, total_deg: float) -> float:
    """转了 `total_deg` 后，终点到起点的直线距离（米）。

    理想圆弧上转过 θ 后，弦长 = 2R·sin(θ/2)。
    角度不是整圈时，这正是「车没回到起点」的距离。
    """
    if radius_m is None or radius_m <= 0:
        return 0.0
    return abs(2.0 * radius_m * math.sin(math.radians(abs(total_deg)) / 2.0))


def summarize(wheelbase_m: float, steer_cmd_rad: float, speed_cmd_mps: float,
              target_deg: float) -> dict:
    """一次性给出某组参数的几何预期，供报告与标定工具使用。"""
    radius = turning_radius(wheelbase_m, steer_cmd_rad)
    omega = yaw_rate_rad(wheelbase_m, steer_cmd_rad, speed_cmd_mps)
    return {
        "wheelbase_m": wheelbase_m,
        "steer_cmd_deg": round(math.degrees(abs(steer_cmd_rad)), 2),
        "radius_m": round(radius, 3) if radius else None,
        "yaw_rate_deg_s": round(math.degrees(omega), 2) if omega else None,
        "seconds_for_target": (round(spin_seconds(wheelbase_m, steer_cmd_rad,
                                                  speed_cmd_mps, target_deg), 1)
                               if omega else None),
        "curve_length_m": (round(abs(math.radians(target_deg)) * radius, 2)
                           if radius else None),
    }


def expected_slip(wheelbase_m: float, track_m: float,
                  steer_rad: float) -> Optional[dict]:
    """四驱同速阿克曼车转弯时的**理论打滑率**。

    为什么这是必然的
    ----------------
    T110 是阿克曼四驱：一个电机经齿轮/皮带带四个轮子，**四轮转速强制相同**。
    而转弯时四个轮子走过的路径长度各不相同：

    1. **内外轮**：同一轴上，内轮走半径 `R − W/2`、外轮走 `R + W/2`。
       速度需求差 `W / R`。四驱同速 → 必有一侧打滑。
    2. **前后轴**：前轴走 `√(R² + L²)`，后轴走 `R`。
       需求差 `√(1 + (L/R)²) − 1`。四驱同速 → 某一轴被拖着走。
    3. **单舵机**：真阿克曼要求内外前轮转角不同
       （`atan(L/(R∓W/2))`），单舵机拉杆只能给一个角，
       近似平行转向 → 前轮额外侧滑。

    实测验证（2026-11-10，δ=0.35 rad、v=0.12 m/s、轴距 0.261、轮距 0.163）：
        理论 29.3%  vs  实测左转 28.4% / 右转 34.5%

    左转几乎完全吻合，说明打滑量**由几何决定，不是偶发故障**。

    返回 dict，`total_pct` 是与 odom/IMU 对比时可直接用的打滑率。
    转角为 0 或参数非法返回 None。
    """
    if wheelbase_m <= 0 or track_m <= 0:
        return None
    radius = turning_radius(wheelbase_m, steer_rad)
    if radius is None or radius <= 0:
        return None
    lr = track_m / radius                              # 内外轮
    fb = math.hypot(radius, wheelbase_m) / radius - 1.0  # 前后轴
    return {
        "radius_m": round(radius, 4),
        "lateral_pct": round(lr * 100, 2),
        "fore_aft_pct": round(fb * 100, 2),
        "total_pct": round((lr + fb) * 100, 2),
    }


def ideal_front_angles(wheelbase_m: float, track_m: float,
                       steer_rad: float) -> Optional[dict]:
    """真阿克曼要求的内外前轮转角，用于说明单舵机的偏差。

    外轮：`atan(L / (R + W/2))`，内轮：`atan(L / (R − W/2))`。
    两者之差就是单舵机无法消除的侧滑角。
    """
    radius = turning_radius(wheelbase_m, steer_rad)
    if radius is None or track_m <= 0:
        return None
    half = track_m / 2.0
    if radius - half <= 0:
        return None
    outer = math.atan(wheelbase_m / (radius + half))
    inner = math.atan(wheelbase_m / (radius - half))
    return {
        "outer_deg": round(math.degrees(outer), 2),
        "inner_deg": round(math.degrees(inner), 2),
        "spread_deg": round(math.degrees(inner - outer), 2),
    }
