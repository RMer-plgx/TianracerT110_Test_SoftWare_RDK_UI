"""12. 里程计。"""

from __future__ import annotations

from core.verdict import CheckResult, Status, judge_all, judge_range

from ._util import run_probe

# 与 checks/imu.py 保持同一个后缀，runner 按它回填
PENDING_SUFFIX = "；频率与静止漂移正常，航向精度待落地互校"


def run(ctx) -> CheckResult:
    result = CheckResult(number=12, title="里程计", status=Status.PASS)
    cfg = ctx.baseline.get("odom", {})
    freq = ctx.baseline.get("frequency", {}).get("odom_hz", {})
    window = float(cfg.get("sample_seconds", 5))

    data = run_probe(ctx, "odom_probe", "/tianracer/odom", str(window),
                     timeout=int(window) + 40)
    if not data or not data.get("ok"):
        result.status = Status.FAIL
        result.summary = "里程计无数据（话题 /tianracer/odom 未发布）"
        return result

    low = float(freq.get("min", 95.0))
    high = float(freq.get("max", 105.0))
    hz = data.get("hz")
    result.add("频率", f"{hz:.2f}" if hz else "未测到", "Hz",
               expected=f"{low}–{high}")
    hz_status = judge_range(hz, low, high, warn_margin=2.0)

    drift = data.get("static_drift_m")
    max_drift = float(cfg.get("static_drift_max_m", 0.01))
    result.add("静止漂移", f"{drift:.4f}" if drift is not None else "?",
               "m", expected=f"< {max_drift} m",
               note=f"{window:.0f} 秒内位移")
    drift_status = judge_range(drift, 0, max_drift)

    start = data.get("start", {})
    end = data.get("end", {})
    result.add("位置 x", f"{end.get('x', 0):.4f}", "m")
    result.add("位置 y", f"{end.get('y', 0):.4f}", "m")

    statuses = [hz_status, drift_status]
    result.status = judge_all(statuses)

    if result.status is Status.FAIL:
        if hz_status is Status.FAIL:
            result.summary = f"频率异常 {hz} Hz（期望 {low}–{high}）"
        else:
            result.summary = f"静止漂移 {drift:.3f} m 超过 {max_drift} m"
    elif result.status is Status.WARN:
        result.summary = "里程计指标接近阈值"
    elif result.status is Status.SKIP:
        missing = []
        if hz is None:
            missing.append("频率")
        if drift is None:
            missing.append("静止漂移")
        result.summary = "未取得数据：" + "、".join(missing)
    else:
        result.summary = f"{hz:.1f} Hz，{window:.0f} 秒静止漂移 {drift:.4f} m"

        # 频率和静止漂移只能说明「有输出、不动时不乱漂」。
        # 里程计推算的航向准不准，必须落地转一整圈与 IMU 比对。
        level = getattr(ctx, "motion_level", "none")
        if level != "none":
            result.status = Status.PENDING
            result.needs_ground = True
            result.summary += PENDING_SUFFIX
        else:
            result.summary += "；本次未做运动测试，行驶航向精度未验证"
    return result
