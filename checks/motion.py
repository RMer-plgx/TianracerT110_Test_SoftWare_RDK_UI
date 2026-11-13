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

import math

from core.geometry import (correct_for_slip, expected_slip, slip_ratio,
                           steer_from_measurement, summarize)
from core.verdict import (CheckResult, Status, judge_all, judge_displacement,
                          judge_distance, judge_max_only, judge_range,
                          judge_repeats)

from ._util import run_probe


def run(ctx) -> CheckResult:
    result = CheckResult(number=20, title="运动控制", status=Status.PASS,
                         phase="motion")
    cfg = ctx.baseline.get("motion", {})
    safety = cfg.get("safety", {})
    max_speed = float(safety.get("max_speed_mps", 0.5))
    max_dur = float(safety.get("max_duration_sec", 10.0))
    level = getattr(ctx, "motion_level", "bench")

    # 电压未知视为禁止运动（审查发现：之前未知即放行）
    voltage_stop = getattr(ctx, "voltage_stop", False)
    voltage_known = getattr(ctx, "voltage_known", False)
    if not voltage_known and not getattr(ctx, "force_motion", False):
        result.status = Status.SKIP
        result.summary = "电压未知，按安全规则跳过全部运动测试"
        result.detail = (
            "无法确认电池电压时不应驱动车辆。\n"
            "请查看第 14 项电压检测的失败原因，或加 --force-motion 显式覆盖（会写进报告）。"
        )
        return result

    if voltage_stop and not getattr(ctx, "force_motion", False):
        result.status = Status.SKIP
        result.summary = (f"电压 {ctx.voltage_v:.2f} V 低于安全阈值，"
                          f"已跳过全部运动测试")
        return result

    # 微脉冲只需要 motion_armed（允许车辆微动），与是否做完整运动测试无关。
    # 这是 D11 的落地：即使运动测试关闭，也要验证底盘到底响不响应。
    if not getattr(ctx, "motion_armed", False):
        result.status = Status.SKIP
        result.summary = "用户未允许车辆微动，无法验证底盘响应"
        result.detail = (
            "本次未做微脉冲。这意味着「电机能不能转」未被验证——\n"
            "历史教训：串口有数据、里程计有 100 Hz 输出，但电调未开，车完全不动。"
        )
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

    # level == "none"：用户只允许微动，不做完整运动测试。
    #
    # 微脉冲能证明「电机能转」，但前进/后退/转向的准确度一次都没测。
    # 所以即使 pulse 通过也只能判 WARN，不能判 PASS——
    # 否则报告会出现绿色的「运动控制 ✅」和「具备交付条件」，
    # 而实际上行驶精度未经任何验证（Round 2 审查发现）。
    if level == "none":
        if pulse_status is Status.PASS:
            result.status = Status.WARN
            result.summary = (
                f"仅验证了电机响应（微脉冲位移 {dist:.4f} m），"
                f"前进/后退/转向均未测"
            )
        else:
            result.status = pulse_status
            result.summary = (
                f"微脉冲位移 {dist:.4f} m，低于阈值" if dist is not None
                else "微脉冲未取得位移数据"
            )
        result.detail = (
            "本次只做了微脉冲，能证明底盘会响应该指令，但无法证明：\n"
            "  - 前进/后退的方向与距离准确\n"
            "  - 左右转向都能动且方向正确\n"
            "  - 左右轮转速差符合转向几何\n\n"
            "要让第 18 项判 PASS，必须选择完整运动测试（架空段或架空+落地）。"
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
        elif result.status is Status.SKIP:
            result.summary = "架空段有项目未验证完，详见明细"
        else:
            result.summary = "架空段存在失败项"
        result.detail = "本次未执行落地段。要验证真实行驶距离，请选择完整测试。"
        return result

    if not bench_ok:
        result.status = Status.FAIL
        result.summary = "架空段未通过，已跳过落地段（避免让异常的车落地行驶）"
        return result

    # 架空段通过，若用户要跑落地段，先提醒把车放下
    # 已请求停止则不弹框，直接结束（审查指出：否则点了停止还会弹框并卡 600 秒）
    if _cancelled(ctx):
        result.status = judge_all(sub_statuses)
        result.summary = "用户手动停止，落地段未执行"
        return result

    pause = getattr(ctx, "pause_for_ground", None)
    if pause is not None:
        answer = pause()
        if answer is not True:
            result.status = judge_all(sub_statuses)
            if _cancelled(ctx):
                result.summary = "用户手动停止，落地段未执行"
                result.detail = "停在落地确认环节。"
            elif answer is None:
                # 超时（None）和用户主动选 No（False）是两回事，
                # 不能把超时写成「用户选择」（审查指出）
                result.summary = "架空段通过；等待落地确认超时，落地段未执行"
                result.detail = "界面等待用户确认是否落地，超时未回应。"
            else:
                result.summary = "架空段通过；用户选择不执行落地段"
                result.detail = "落地段已跳过。"
            return result

    # ---- 落地段 ----
    # spin 段必须从完整的 motion 配置里取，不能从 ground 段取：
    # 曾经把 ground_cfg 传给 _run_spin，而它读的是 cfg["spin"]，
    # 于是每一台车都永远走「配置缺失，未执行」，转圈一次也没跑过。
    ground_cfg = cfg.get("ground", {})
    ground_statuses, spin_ok = _run_ground(
        ctx, ground_cfg, result, max_speed, max_dur, safety,
        spin_cfg=cfg.get("spin", {}),
        wheelbase=float(cfg.get("wheelbase_m", 0) or 0),
        track=float(cfg.get("track_m", 0) or 0))
    sub_statuses.extend(ground_statuses)

    # 把「转向互校是否验证过」传给上层：IMU / 里程计这两项
    # 在架空段只能测频率，必须靠这里才能真正判 PASS
    ctx.spin_verified = spin_ok

    result.status = judge_all(sub_statuses)
    if result.status is Status.PASS:
        result.summary = "架空段与落地段全部通过"
    elif result.status is Status.WARN:
        result.summary = "运动测试通过，但速度或转向存在偏差"
    elif result.status is Status.SKIP:
        # SKIP 与 FAIL 是两回事：前者「有项没验证完」，后者「验了且不合格」。
        # 混写成「存在失败项」会让用户去找一个不存在的故障
        # （2026-11-07 实车报告即为此例：落地段四个子项全 PASS，只因转圈缺项
        #   被判 SKIP，摘要却写「存在失败项」）。
        result.summary = "运动测试有项目未验证完，详见明细"
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
    if _cancelled(ctx):
        return statuses, False
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
        ok = ok and st is Status.PASS   # SKIP 也是未验证，不能带着它进落地段

    reverse = _run_motion(
        ctx, speed=float(cfg.get("reverse_speed", -0.10)), steer=0.0,
        duration=float(cfg.get("reverse_duration", 2.0)),
        label="bench_reverse", max_speed=max_speed, max_dur=max_dur,
    )
    if _cancelled(ctx):
        return statuses, False
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
        ok = ok and st is Status.PASS
        # 方向校验：后退应与前进相反。架空时用 dyaw 不可靠，只看位移存在

    # 左右转向各测一次——只测一个方向会漏掉单侧电机故障
    #
    # 必须叠加一个小的前进速度：speed=0 时轮子不转，编码器无变化，
    # 也就不会有 yaw 变化，正常车也会被误判失败（审查发现）。
    # 架空时轮子空转，里程计由编码器推算，所以仍能观察 yaw 变化。
    steer = float(cfg.get("steer_angle", 0.30))
    steer_dur = float(cfg.get("steer_duration", 1.5))
    steer_speed = float(cfg.get("steer_speed", 0.05))
    yaw_changes = []
    for label, angle in (("bench_steer_left", steer), ("bench_steer_right", -steer)):
        if _cancelled(ctx):
            return statuses, False
        data = _run_motion(
            ctx, speed=steer_speed, steer=angle, duration=steer_dur,
            label=label, max_speed=max_speed, max_dur=max_dur,
        )
        if _cancelled(ctx):
            return statuses, False
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


def _run_ground(ctx, cfg, result, max_speed, max_dur, safety=None,
                spin_cfg=None, wheelbase=0.0, track=0.0):
    """落地段：真实行驶，校验距离、转向弧度、航向互校。

    cfg 是 baseline 的 motion.ground 段；spin_cfg 是 motion.spin 段。
    两者分开传入，避免把子段当整段用（历史上正是这里出错，转圈从未执行）。

    返回 (statuses, spin_verified)。
    spin_verified 为 True 表示左右两圈都转成功且偏差合格，
    这时 IMU / 里程计才能从「待落地验证」转为 PASS。
    """
    safety = safety or {}
    statuses = []

    for label, key, speed_key, dur_key, exp_key, tol_key in (
        ("落地·前进", "forward", "forward_speed", "forward_duration",
         "forward_expected_m", "forward_tolerance"),
        ("落地·后退", "reverse", "reverse_speed", "reverse_duration",
         "reverse_expected_m", "reverse_tolerance"),
    ):
        if _cancelled(ctx):
            return statuses, False
        speed = float(cfg.get(speed_key, 0.10))
        duration = float(cfg.get(dur_key, 3.0))
        expected = float(cfg.get(exp_key, 0.30))
        tolerance = float(cfg.get(tol_key, 0.20))

        data = _run_motion(ctx, speed=speed, steer=0.0, duration=duration,
                           label=f"ground_{key}", max_speed=max_speed,
                           max_dur=max_dur)
        if _cancelled(ctx):
            return statuses, False
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
            # 探针的 avg_speed 是速率（hypot 恒为正），不是带符号的分量速度。
            # 拿 -0.10 去对照只会让人以为符号错了，所以期望值也按速率显示。
            result.add(f"{label}实测速度", f"{avg:.3f}", "m/s",
                       expected=f"{abs(speed):.2f}",
                       note=f"指令 {speed:+.2f} m/s，这里比对速率")

    # 转向弧度：左右各一次，用 judge_repeats 取最差
    steer = float(cfg.get("steer_angle", 0.30))
    dur = float(cfg.get("steer_duration", 2.0))
    expected_yaw = float(cfg.get("steer_expected_yaw_deg", 11.0))
    tol_yaw = float(cfg.get("steer_tolerance_deg", 6.0))
    magnitudes = []
    for label, angle in (("落地·左转", steer), ("落地·右转", -steer)):
        if _cancelled(ctx):
            return statuses, False
        data = _run_motion(ctx, speed=float(cfg.get("forward_speed", 0.10)),
                           steer=angle, duration=dur, label=label,
                           max_speed=max_speed, max_dur=max_dur)
        if _cancelled(ctx):
            return statuses, False
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

    # ---- 转向互校：左右各转一整圈，比对 odom 与 IMU 的航向 ----
    spin_statuses, spin_ok = _run_spin(ctx, spin_cfg, result, safety,
                                       wheelbase=wheelbase, track=track)
    statuses.extend(spin_statuses)

    return statuses, spin_ok


def _run_spin(ctx, spin_cfg, result, safety, wheelbase=0.0, track=0.0):
    """转整圈做航向互校。返回 (statuses, verified)。

    spin_cfg 是 baseline 的 motion.spin 段本身（不是整个 motion）。
    wheelbase / track 是 motion 顶层的轴距与轮距（米）。
    大于 0 时额外算理论角速度、打滑率与反推转角。
    verified 只在左右两圈都真的转成功、且偏差合格时为 True。
    这个值决定 IMU / 里程计这两项能不能从「待落地验证」转 PASS。
    """
    spin = spin_cfg or {}
    if not spin:
        # 配置里没有这一段（老版本 baseline），视为未验证
        result.add("转向互校", "配置缺失，未执行")
        return [Status.SKIP], False

    speed = float(spin.get("speed", 0.12))
    steer = float(spin.get("steer_angle", 0.35))
    target = float(spin.get("target_yaw_deg", 360.0))
    max_seconds = float(spin.get("max_seconds", 90))
    tol = float(spin.get("yaw_tolerance_deg", 15.0))
    warn_at = float(spin.get("yaw_warn_deg", 30.0))
    closure_warn = float(spin.get("closure_warn_m", 0.30))
    # 打滑率允许偏离理论值的幅度（百分点）。
    # 四驱同速的阿克曼车转弯必然打滑，理论值由几何算出；
    # 超出这个带宽才说明轮胎磨损、地面油污或某轮卡死。
    slip_tol = float(spin.get("slip_tolerance_pct", 15.0))
    # 轮距（左右轮中心距）由调用方从 motion 顶层传入。
    # 曾经写成 `spin.get("track_m")` —— 而 baseline 里它在 motion.track_m，
    # 与 spin 平级，于是永远读到 0，报告一直显示
    # 「缺轴距/轮距参数，无法与理论值对比」（2026-11-10 实车报告即为此）。

    # 理论打滑率只看几何，与具体哪一次跑无关，循环外算一次。
    theory_slip = expected_slip(wheelbase, track, steer) if track > 0 else None
    if theory_slip:
        result.add("转向几何", f"理论打滑 {theory_slip['total_pct']:.0f}%", "",
                   note=(f"轴距 {wheelbase:.3f} m、轮距 {track:.3f} m、"
                         f"转角 {math.degrees(abs(steer)):.1f}°；"
                         f"内外轮 {theory_slip['lateral_pct']:.0f}% + "
                         f"前后轴 {theory_slip['fore_aft_pct']:.0f}%。"
                         f"四驱同速导致，不是故障"))

    hard_speed = float(safety.get("spin_max_speed_mps", 0.25))
    hard_seconds = float(safety.get("spin_max_seconds", 120.0))
    # 配置只能收紧，不能放宽硬上限
    speed = min(speed, hard_speed)
    max_seconds = min(max_seconds, hard_seconds)

    statuses = []
    verified_all = True

    for side, angle, label in (("左", steer, "spin_left"),
                               ("右", -steer, "spin_right")):
        if _cancelled(ctx):
            statuses.append(Status.SKIP)
            verified_all = False
            continue

        signed_target = target if angle > 0 else -target
        data = run_probe(
            ctx, "spin_probe",
            f"{speed}", f"{angle}", f"{signed_target}", f"{max_seconds}", label,
            ctx.profile.ros.topic("cmd"), ctx.profile.ros.topic("odom"),
            ctx.profile.ros.topic("imu"),
            timeout=int(max_seconds) + 40,
        )

        if _cancelled(ctx):
            statuses.append(Status.SKIP)
            verified_all = False
            ctx.say(f"       ■ {side}转被用户中断")
            continue

        if not data or not data.get("ok"):
            reason = (data or {}).get("reason", "")
            detail = (data or {}).get("detail", "")
            result.add(f"{side}转整圈", f"失败（{reason or '无返回'}）")
            if detail:
                ctx.say(f"       ⚠ {side}转：{detail}")
            statuses.append(Status.FAIL)
            verified_all = False
            continue

        if data.get("aborted"):
            result.add(f"{side}转整圈", "用户中断")
            statuses.append(Status.SKIP)
            verified_all = False
            continue

        imu_deg = data.get("imu_yaw_deg")
        odom_deg = data.get("odom_yaw_deg")
        diff = data.get("yaw_diff_deg")
        closure = data.get("closure_m")
        duration = data.get("duration")

        # 打滑率提前算：主判据与几何反推都要用。
        # 旧版只在几何反推那段里算，导致判定刚拿不到。
        slip = slip_ratio(odom_deg, imu_deg) if (odom_deg is not None and imu_deg) else None
        slip_pct = slip * 100.0 if slip is not None else None

        result.add(f"{side}转整圈耗时", f"{duration:.1f}" if duration else "?", "s")
        result.add(f"{side}转 IMU 累积", f"{imu_deg:+.1f}", "°",
                   expected=f"±{target:.0f}°") if imu_deg is not None else \
            result.add(f"{side}转 IMU 累积", "未取得")
        result.add(f"{side}转 odom 累积", f"{odom_deg:+.1f}", "°",
                   expected=f"±{target:.0f}°") if odom_deg is not None else \
            result.add(f"{side}转 odom 累积", "未取得")

        # ---- 主判据：车体姿态确实转够了 ----
        # 这是唯一直接测量、不受打滑污染的指标：IMU 测的是车体姿态角。
        # 旧版拿 IMU 与 odom 的差值判 PASS/FAIL，而四驱同速的阿克曼车
        # **必然**打滑 29%，差值常年 100°+ —— 等于用一个物理上达不到的
        # 标准判它失败。2026-11-10 实测：IMU 转 360.68°（过冲 0.68°，准），
        # 但 odom 463.28°，差 102.6°。
        if imu_deg is not None:
            imu_err = abs(abs(imu_deg) - abs(target))
            result.add(f"{side}转姿态误差", f"{imu_err:.2f}", "°",
                       expected=f"≤ {tol:.0f}°",
                       note=(f"IMU 实测 {imu_deg:+.1f}° 相对目标 "
                             f"{signed_target:+.0f}° 的偏差"))
            st = judge_max_only(imu_err, tol, warn_at)
            statuses.append(st)
            if st is not Status.PASS:
                verified_all = False
        else:
            statuses.append(Status.SKIP)
            verified_all = False

        # ---- 打滑率：与几何理论值对比，不是「越小越好」----
        # 四驱同速 + 单舵机的结构决定了转弯必然打滑，理论值由
        # `expected_slip()` 按轴距/轮距/转角算出。偏离过多才说明异常
        # （轮胎磨损、地面油污、某轮卡死）。
        #
        # 2026-11-13（ADR-0008）：打滑率**不再否掉 verified**。
        # 它主要反映地面摩擦（同一台车换块地能差 10pp 以上），
        # 而 verified 代表「车体姿态精度已验证」，是车本身的能力。
        # 两者绑在同一个结论里，等于让地面决定一台车能不能交付。
        # 实测：姿态误差 0.42~0.96°（容差 3°）、IMU ±360.8~361.0°，
        # 车确实转满了，但打滑率 45~52% 超理论值 → 连带 IMU/里程计 PENDING。
        # 现在打滑率仍判 WARN（保留在 statuses 里影响第 18 项整体状态），
        # 但不影响 verified。
        if slip_pct is not None and theory_slip is not None:
            dev = slip_pct - theory_slip["total_pct"]
            result.add(
                f"{side}转打滑率", f"{slip_pct:.0f}", "%",
                expected=f"理论 {theory_slip['total_pct']:.0f}% ± {slip_tol:.0f}",
                note=(f"内外轮差 {theory_slip['lateral_pct']:.0f}% + "
                      f"前后轴差 {theory_slip['fore_aft_pct']:.0f}%；"
                      f"四驱同速导致，不是故障；此值反映地面摩擦，"
                      f"不影响 IMU 姿态精度判定"))
            st = judge_max_only(abs(dev), slip_tol, slip_tol * 2)
            statuses.append(st)
            # WARN 不否 verified：WARN 表示「地面摩擦与标定时不同」，
            #   是环境差异，不是车的能力问题（ADR-0008）。
            # FAIL 仍否 verified：judge_max_only 的 FAIL 阈值是 slip_tol*2，
            #   即偏离超过 30 个百分点（如打滑 200%）。那不是地面能造成的，
            #   而是轮胎脱落 / 某轮卡死 / odom 严重失真这类真故障。
            if st is Status.FAIL:
                verified_all = False
        elif slip_pct is not None:
            # 没有几何参数时只记录，不判定
            result.add(f"{side}转打滑率", f"{slip_pct:.0f}", "%",
                       note="缺轴距/轮距参数，无法与理论值对比")

        # ---- 闭合误差：只记录，不判定 ----
        # 它几乎完全由打滑决定：转 360° 后位置漂移 ≈ R × 打滑率。
        # 实测 1.12~1.27 m，与「R=0.715 × 29% ≈ 0.21 m」同量级但不相等，
        # 因为轨迹不是理想圆弧。把它当合格线判会永远 FAIL。
        if closure is not None:
            # 只记录不判定。它由打滑与轨迹形变共同决定，没有可靠的解析估计：
            # 2026-11-10 实测 1.12~1.27 m，而 R×打滑率只给出 0.21 m（差 5 倍），
            # 因为位移来自转弯中心漂移，不是半径差。留作人工判断与历史对比。
            result.add(
                f"{side}转闭合误差", f"{closure:.3f}", "m",
                note="转完一圈回到起点的距离；四驱同速必打滑，此项不参与判定")

        # ---- 几何反推：把「指令转向角」换成「真实转向角」----
        # MCU 不回传舵机角度，只能用 IMU 实测角速度间接反推。
        # 这是在不改固件的前提下唯一能量化「舵机到底差多少」的办法。
        if wheelbase > 0 and duration and duration > 0 and imu_deg:
            omega_deg = abs(imu_deg) / duration
            v_real = correct_for_slip(abs(speed), slip)
            real_steer = steer_from_measurement(wheelbase, v_real, omega_deg)
            theo = summarize(wheelbase, abs(steer), abs(speed), abs(target))

            if theo.get("yaw_rate_deg_s"):
                result.add(
                    f"{side}转理论角速度", f"{theo['yaw_rate_deg_s']:.2f}", "°/s",
                    note=(f"按轴距 {wheelbase:.3f} m、转角 "
                          f"{theo['steer_cmd_deg']:.1f}° 算的理论值"))
            result.add(f"{side}转实测角速度", f"{omega_deg:.2f}", "°/s")

            if real_steer is not None:
                real_deg = math.degrees(real_steer)
                cmd_deg = math.degrees(abs(steer))
                result.add(
                    f"{side}转反推转角", f"{real_deg:.2f}", "°",
                    expected=f"指令 {cmd_deg:.2f}°",
                    note="由实测角速度与打滑修正反推，不是直接测量")
                # 不参与判定：打滑修正是一阶近似，且可能受 odom yaw 来源影响。
                # 先公布在报告里积累数据，确认阈值后再决定是否计入。

        # 没转够一圈（异常提前停）不能算验证过。
        #
        # 统一记 SKIP 而不是 WARN：这里的语义是「没验证完」，
        # 不是「验证了但有偏差」。记 WARN 会让报告多一条黄色条目，
        # 读者容易误以为「只是有警告」，实际是根本没测到。
        if not data.get("reached"):
            anomaly = data.get("anomaly") or "未转到目标角度"
            result.add(f"{side}转未完成", anomaly)
            statuses.append(Status.SKIP)
            verified_all = False

    # 返回值的两个用途：
    #   statuses → judge_all(statuses) 决定第 18 项整体状态（打滑率 WARN 会体现在这里）
    #   第二项   → 决定 IMU/里程计能否从 PENDING 转 PASS
    #
    # 第二项**只看 verified_all**，不再要求所有 status 都是 PASS。
    # 否则打滑率一 WARN，IMU/里程计就永远保持待验证（ADR-0008）。
    return statuses, verified_all


# ---------------------------------------------------------------------------
# 底层调用
# ---------------------------------------------------------------------------

def _cancelled(ctx) -> bool:
    return bool(getattr(ctx, "cancel_requested", False))


def emergency_stop(ctx, reason: str = "用户请求停止") -> None:
    """立即向底盘发零速。

    关键：用**独立的 SSH 连接**，不复用 ctx.remote。
    因为用户点停止时，主通道可能正阻塞在 run_probe 上（等探针跑完），
    那段时间内用主通道发指令是发不出去的。

    发两帧零速（前进 0、转向 0），确保底盘收到。
    """
    from core.remote import ros
    from core.ssh import Remote

    ctx.say(f"       ■ 紧急停车（{reason}）")
    try:
        quick = Remote(ctx.remote.host, ctx.remote.user,
                       ctx.remote._password, timeout=6)
        quick.connect()
        try:
            # 必须先让正在旋转的探针自己停下。
            # 转圈探针以 20 Hz 持续发指令，这里另开连接发的零速
            # 会在 50 ms 内被它覆盖 —— 光发零速是停不住车的。
            quick.run("touch /tmp/tt_spin_abort", timeout=8, record=False)
            for _ in range(2):
                quick.run(
                    ros(
                        f"timeout 3 rostopic pub -1 "
                        f"{ctx.profile.ros.topic('cmd')} "
                        "ackermann_msgs/AckermannDrive "
                        "'{speed: 0.0, steering_angle: 0.0}'",
                        ctx.profile,
                    ),
                    timeout=12, record=False,
                )
        finally:
            quick.close()
        ctx.say("       ■ 已通知探针停车，零速指令已发送")
    except Exception as exc:
        ctx.say(f"       ⚠ 紧急停车失败：{type(exc).__name__}: {exc}")
        ctx.say("         请立即用遥控器接管")


def _run_motion(ctx, speed: float, steer: float, duration: float,
                label: str, max_speed: float, max_dur: float):
    """跑一次运动探针，返回它的 JSON；失败返回 None。"""
    # 已请求停止：不再发起任何一段。
    # 否则用户点了停止，车还会继续走完剩余的 8 段（审查指出）。
    if _cancelled(ctx):
        return None

    data = run_probe(
        ctx, "motion_probe",
        f"{speed}", f"{steer}", f"{duration}", label,
        f"{max_speed}", f"{max_dur}",
        ctx.profile.ros.topic("cmd"), ctx.profile.ros.topic("odom"),
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
