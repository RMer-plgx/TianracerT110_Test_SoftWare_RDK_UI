"""18. 运动控制（两段式）。

段位划分（用户要求）：
  bench  架空段 —— 轮子离地，安全，任何场地可做。
                验证「电机能转、编码器有反馈、方向正确、左右对称」。
  ground 落地段 —— 车在开阔平地，需人确认安全。
                验证「真实行驶距离、速度准确度、转向弧度」。

顺序固定：微脉冲 → bench → ground。
任一段失败，后续段不再执行。

安全：
- 微脉冲和每段都在后台静默执行，不占交互界面
- 速度/时长由 baseline.yaml 给出，探针内部还有硬性上限二次校验
- 电压低于阈值时整个项目跳过
"""

from __future__ import annotations

from core.verdict import (CheckResult, Status, judge_all, judge_displacement,
                          judge_distance, judge_range, judge_repeats)

from ._util import run_probe


def run(ctx) -> CheckResult:
    result = CheckResult(number=18, title="运动控制", status=Status.PASS,
                         phase="motion")
    cfg = ctx.baseline.get("motion", {})
    safety = cfg.get("safety", {})
    max_speed = float(safety.get("max_speed_mps", 0.5))
    max_dur = float(safety.get("max_duration_sec", 10.0))
    level = getattr(ctx, "motion_level", "bench")

    # 电压不足时不允许让车动
    if getattr(ctx, "voltage_stop", False):
        result.status = Status.SKIP
        result.summary = (f"电压 {ctx.voltage_v:.2f} V 低于安全阈值，"
                          f"已跳过全部运动测试")
        return result

    if not getattr(ctx, "motion_armed", False):
        result.status = Status.SKIP
        result.summary = "用户未确认运动测试安全条件"
        return result

    pulse_cfg = cfg.get("micro_pulse", {})
    sub_statuses = []

    # ---- 微脉冲：判断底盘到底响不响应 ----
    pulse = _run_motion(
        ctx, speed=float(pulse_cfg.get("speed", 0.05)),
        steer=0.0, duration=float(pulse_cfg.get("duration", 0.3)),
        label="micro_pulse", max_speed=max_speed, max_dur=max_dur,
    )
    if pulse is None:
        result.status = Status.FAIL
        result.summary = "微脉冲无返回，无法确认底盘可控制"
        return result

    dist = pulse.get("dist_m")
    min_dist = float(pulse_cfg.get("min_displacement_m", 0.0005))
    pulse_status = judge_displacement(dist, min_dist)
    sub_statuses.append(pulse_status)
    result.add("微脉冲位移", f"{dist:.4f}" if dist is not None else "?",
               "m", expected=f"≥ {min_dist} m",
               note="0.3s × 0.05m/s，理论约 1.5 cm")

    if pulse_status is Status.FAIL:
        result.status = Status.FAIL
        result.summary = ("微脉冲发出后里程计无位移——底盘未响应。"
                          "最常见原因是电调（ESC）电源开关未打开。")
        result.detail = (
            "排查顺序：\n"
            "1. 电调电源开关是否在开的位置\n"
            "2. 电调是否处于 Li 电模式、F/R 模式\n"
            "3. 电池是否电量充足（本次未触发电压告警则此项可排除）\n"
            "4. 遥控器是否抢占了控制权\n\n"
            "注意：串口有数据 ≠ 电机能转。上位机↔主控通信正常，"
            "不代表主控↔电机通了。"
        )
        return result

    # ---- 架空段 ----
    bench_cfg = cfg.get("bench", {})
    bench_statuses, bench_ok = _run_bench(ctx, bench_cfg, result, max_speed, max_dur)
    sub_statuses.extend(bench_statuses)

    if level != "full":
        result.status = judge_all(sub_statuses)
        if result.status is Status.PASS:
            result.summary = "架空段通过（微脉冲 + 前进/后退/左右转向均响应）"
        elif result.status is Status.WARN:
            result.summary = "架空段基本通过，但存在需关注的偏差"
        else:
            result.summary = "架空段存在失败项"
        result.detail = "本次未执行落地段。要验证真实行驶距离，请选择完整测试。"
        return result

    if not bench_ok:
        result.status = Status.FAIL
        result.summary = "架空段未通过，已跳过落地段（避免让异常的车落地行驶）"
        return result

    # 架空段通过，若用户要跑落地段，先提醒把车放下
    pause = getattr(ctx, "pause_for_ground", None)
    if pause is not None:
        if not pause():
            result.status = judge_all(sub_statuses)
            result.summary = "架空段通过；用户选择不执行落地段"
            result.detail = "落地段已跳过。"
            return result

    # ---- 落地段 ----
    ground_cfg = cfg.get("ground", {})
    ground_statuses = _run_ground(ctx, ground_cfg, result, max_speed, max_dur)
    sub_statuses.extend(ground_statuses)

    result.status = judge_all(sub_statuses)
    if result.status is Status.PASS:
        result.summary = "架空段与落地段全部通过"
    elif result.status is Status.WARN:
        result.summary = "运动测试通过，但速度或转向存在偏差"
    else:
        result.summary = "运动测试存在失败项"
    return result


# ---------------------------------------------------------------------------
# 分段执行
# ---------------------------------------------------------------------------

def _run_bench(ctx, cfg, result, max_speed, max_dur):
    """架空段：轮子空转，只验证「响应」，不校验行驶精度。"""
    statuses = []
    min_dist = float(cfg.get("min_displacement_m", 0.005))
    ok = True

    forward = _run_motion(
        ctx, speed=float(cfg.get("forward_speed", 0.10)), steer=0.0,
        duration=float(cfg.get("forward_duration", 2.0)),
        label="bench_forward", max_speed=max_speed, max_dur=max_dur,
    )
    if forward is None:
        result.add("架空·前进", "无返回")
        statuses.append(Status.FAIL)
        ok = False
    else:
        d = forward.get("dist_m")
        st = judge_displacement(d, min_dist)
        statuses.append(st)
        result.add("架空·前进位移", f"{d:.4f}" if d is not None else "?", "m",
                   expected=f"≥ {min_dist} m")
        ok = ok and st is not Status.FAIL

    reverse = _run_motion(
        ctx, speed=float(cfg.get("reverse_speed", -0.10)), steer=0.0,
        duration=float(cfg.get("reverse_duration", 2.0)),
        label="bench_reverse", max_speed=max_speed, max_dur=max_dur,
    )
    if reverse is None:
        result.add("架空·后退", "无返回")
        statuses.append(Status.FAIL)
        ok = False
    else:
        d = reverse.get("dist_m")
        st = judge_displacement(d, min_dist)
        statuses.append(st)
        result.add("架空·后退位移", f"{d:.4f}" if d is not None else "?", "m",
                   expected=f"≥ {min_dist} m")
        ok = ok and st is not Status.FAIL
        # 方向校验：后退应与前进相反。架空时用 dyaw 不可靠，只看位移存在

    # 左右转向各测一次——只测一个方向会漏掉单侧电机故障
    steer = float(cfg.get("steer_angle", 0.30))
    steer_dur = float(cfg.get("steer_duration", 1.5))
    yaw_changes = []
    for label, angle in (("bench_steer_left", steer), ("bench_steer_right", -steer)):
        data = _run_motion(
            ctx, speed=0.0, steer=angle, duration=steer_dur,
            label=label, max_speed=max_speed, max_dur=max_dur,
        )
        side = "左" if angle > 0 else "右"
        if data is None:
            result.add(f"架空·{side}转", "无返回")
            statuses.append(Status.FAIL)
            ok = False
            continue
        dyaw = data.get("dyaw_deg")
        yaw_changes.append(dyaw)
        result.add(f"架空·{side}转 yaw 变化",
                   f"{dyaw:+.2f}" if dyaw is not None else "?", "°")
        if dyaw is None or abs(dyaw) < 0.3:
            statuses.append(Status.FAIL)
            ok = False

    if len(yaw_changes) == 2 and all(v is not None for v in yaw_changes):
        left, right = yaw_changes
        if left is not None and right is not None:
            if left == 0 or right == 0:
                statuses.append(Status.FAIL)
                ok = False
            elif (left > 0) == (right > 0):
                # 左右转向 yaw 变化同向 = 有一侧反了
                statuses.append(Status.FAIL)
                ok = False
                result.add("转向方向", "左右同向（异常）",
                           note="两个方向应产生相反的 yaw 变化")
            else:
                result.add("转向方向", "左右相反（正确）")

    return statuses, ok


def _run_ground(ctx, cfg, result, max_speed, max_dur):
    """落地段：真实行驶，校验距离与转向弧度。"""
    statuses = []

    for label, key, speed_key, dur_key, exp_key, tol_key in (
        ("落地·前进", "forward", "forward_speed", "forward_duration",
         "forward_expected_m", "forward_tolerance"),
        ("落地·后退", "reverse", "reverse_speed", "reverse_duration",
         "reverse_expected_m", "reverse_tolerance"),
    ):
        speed = float(cfg.get(speed_key, 0.10))
        duration = float(cfg.get(dur_key, 3.0))
        expected = float(cfg.get(exp_key, 0.30))
        tolerance = float(cfg.get(tol_key, 0.20))

        data = _run_motion(ctx, speed=speed, steer=0.0, duration=duration,
                           label=f"ground_{key}", max_speed=max_speed,
                           max_dur=max_dur)
        if data is None:
            result.add(label, "无返回")
            statuses.append(Status.FAIL)
            continue

        dist = data.get("dist_m")
        statuses.append(judge_distance(dist, expected, tolerance))
        result.add(f"{label}距离", f"{dist:.4f}" if dist is not None else "?", "m",
                   expected=f"{expected} ± {tolerance:.0%}")
        avg = data.get("avg_speed")
        if avg is not None:
            result.add(f"{label}实测速度", f"{avg:.3f}", "m/s",
                       expected=f"{speed:.2f}")

    # 转向弧度：左右各一次，用 judge_repeats 取最差
    steer = float(cfg.get("steer_angle", 0.30))
    dur = float(cfg.get("steer_duration", 2.0))
    expected_yaw = float(cfg.get("steer_expected_yaw_deg", 11.0))
    tol_yaw = float(cfg.get("steer_tolerance_deg", 6.0))
    magnitudes = []
    for label, angle in (("落地·左转", steer), ("落地·右转", -steer)):
        data = _run_motion(ctx, speed=float(cfg.get("forward_speed", 0.10)),
                           steer=angle, duration=dur, label=label,
                           max_speed=max_speed, max_dur=max_dur)
        if data is None:
            result.add(label, "无返回")
            magnitudes.append(None)
            continue
        dyaw = data.get("dyaw_deg")
        result.add(f"{label} yaw", f"{dyaw:+.2f}" if dyaw is not None else "?", "°",
                   expected=f"|±{expected_yaw}°| ± {tol_yaw}°")
        magnitudes.append(abs(dyaw) if dyaw is not None else None)

    low = max(0.0, expected_yaw - tol_yaw)
    high = expected_yaw + tol_yaw
    statuses.append(judge_repeats(magnitudes, low, high))
    return statuses


# ---------------------------------------------------------------------------
# 底层调用
# ---------------------------------------------------------------------------

def _run_motion(ctx, speed: float, steer: float, duration: float,
                label: str, max_speed: float, max_dur: float):
    """跑一次运动探针，返回它的 JSON；失败返回 None。"""
    data = run_probe(
        ctx, "motion_probe",
        f"{speed}", f"{steer}", f"{duration}", label,
        f"{max_speed}", f"{max_dur}",
        timeout=int(duration) + 45,
    )
    if not data or not data.get("ok"):
        if data:
            reason = data.get("reason", "")
            if reason == "no_subscriber":
                ctx.say("       ⚠ 没有节点订阅 ackermann_cmd，底盘可能未启动")
            elif reason == "safety_limit":
                ctx.say(f"       ⚠ {data.get('detail', '参数超限')}")
        return None
    return data
