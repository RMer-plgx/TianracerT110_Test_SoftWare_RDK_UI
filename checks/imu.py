"""8. IMU。"""

from __future__ import annotations

from core.verdict import CheckResult, Status, judge_all, judge_range

from ._util import measure_hz, run_probe

# 架空段测完后追加的固定后缀。runner 在落地段结束时按这个字串回填，
# 所以它必须是常量、不能各处手写。
PENDING_SUFFIX = "；频率与静态指标正常，航向精度待落地互校"


def run(ctx) -> CheckResult:
    result = CheckResult(number=8, title="IMU", status=Status.PASS)
    cfg = ctx.baseline.get("imu", {})
    freq = ctx.baseline.get("frequency", {}).get("imu_hz", {})

    hz = measure_hz(ctx, "/tianracer/imu_raw", seconds=8)
    low = float(freq.get("min", 45.0))
    high = float(freq.get("max", 55.0))
    result.add("频率", f"{hz:.2f}" if hz is not None else "无数据", "Hz",
               expected=f"{low}–{high}")
    hz_status = judge_range(hz, low, high, warn_margin=2.0)

    az = roll = pitch = None
    data = run_probe(ctx, "imu_probe", "/tianracer/imu_raw", "5")
    accel_status = gyro_status = tilt_status = Status.SKIP

    if data and data.get("ok"):
        accel = data.get("accel", {})
        result.add("accel x", f"{accel.get('x', 0):.3f}", "m/s²")
        result.add("accel y", f"{accel.get('y', 0):.3f}", "m/s²")

        az = accel.get("z")
        z_min = float(cfg.get("accel_z_min", 9.4))
        z_max = float(cfg.get("accel_z_max", 10.6))
        warn_high = float(cfg.get("accel_z_warn_high", 10.3))
        result.add("accel z", f"{az:.3f}" if az is not None else "?", "m/s²",
                   expected=f"{z_min}–{z_max}",
                   note="静置应接近 9.8")
        accel_status = judge_range(az, z_min, z_max,
                                   warn_margin=max(0.0, z_max - warn_high))

        norm = data.get("accel_norm")
        if norm is not None:
            result.add("accel 模长", f"{norm:.3f}", "m/s²")

        gyro = data.get("gyro", {})
        max_gyro = max((abs(gyro.get(k, 0)) for k in ("x", "y", "z")), default=None)
        limit_gyro = float(cfg.get("gyro_static_max", 0.05))
        result.add("最大角速度",
                   f"{max_gyro:.4f}" if max_gyro is not None else "?",
                   "rad/s", expected=f"< {limit_gyro}")
        gyro_status = judge_range(max_gyro, 0, limit_gyro)

        roll = data.get("roll_deg")
        pitch = data.get("pitch_deg")
        limit_tilt = float(cfg.get("roll_pitch_max_deg", 5.0))
        if roll is not None and pitch is not None:
            result.add("roll", f"{roll:.2f}", "°", expected=f"|·| < {limit_tilt}")
            result.add("pitch", f"{pitch:.2f}", "°", expected=f"|·| < {limit_tilt}")
            tilt_status = judge_range(max(abs(roll), abs(pitch)), 0, limit_tilt)
    else:
        result.add("姿态数据", "探针未取得数据")

    statuses = [hz_status, accel_status, gyro_status, tilt_status]

    # 用 judge_all 统一收口。手写 if/elif 链会漏掉
    # 「部分 PASS + 部分 SKIP」这种组合，导致探针全失败仍报 PASS（审查发现）。
    result.status = judge_all(statuses)

    if result.status is Status.FAIL:
        if hz_status is Status.FAIL:
            result.summary = f"频率异常：{hz} Hz（期望 {low}–{high}）"
        elif accel_status is Status.FAIL:
            result.summary = "加速度 z 轴偏离 9.8 过多，IMU 可能装歪或未标定"
        elif gyro_status is Status.FAIL:
            result.summary = "静止时角速度偏大，IMU 有噪声或车在动"
        else:
            result.summary = "车体姿态倾斜过大，检查 IMU 安装"
    elif result.status is Status.WARN:
        if accel_status is Status.WARN:
            result.summary = (f"z 轴 {az:.2f} m/s² 偏高（标称 9.8），标度待确认"
                              if az is not None else "IMU 数据接近阈值")
        else:
            result.summary = "IMU 数据接近阈值，需关注"
    elif result.status is Status.SKIP:
        # 指出具体哪几项没验证，否则读者不知道是频率还是姿态没测到
        not_verified = []
        if hz_status is Status.SKIP:
            not_verified.append("频率")
        if accel_status is Status.SKIP:
            not_verified.append("加速度")
        if gyro_status is Status.SKIP:
            not_verified.append("角速度")
        if tilt_status is Status.SKIP:
            not_verified.append("姿态")
        result.summary = "未取得数据：" + "、".join(not_verified)
    else:
        parts = []
        if hz is not None:
            parts.append(f"{hz:.1f} Hz")
        if az is not None:
            parts.append(f"z={az:.2f}")
        if roll is not None and pitch is not None:
            parts.append(f"roll {roll:.2f}° / pitch {pitch:.2f}°")
        result.summary = "，".join(parts) if parts else "IMU 正常"

        # 架空段只能证明「有数据、频率对、静止不漂」。
        # 陀螺仪准不准，必须落地转一整圈、与里程计比对航向才知道。
        # 用户 2026-11-03：这种「测了一半」不能直接标绿。
        level = getattr(ctx, "motion_level", "none")
        if level != "none":
            result.status = Status.PENDING
            result.needs_ground = True
            result.summary += PENDING_SUFFIX
        else:
            result.summary += "；本次未做运动测试，航向精度未验证"

    return result
