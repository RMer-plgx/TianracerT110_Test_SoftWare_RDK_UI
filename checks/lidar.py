"""9. 激光雷达。

关键：LakiBeam1 物理视场约 ±120~135°，后方为 inf/nan 属正常。
不能用「四扇区都有点」判定，否则每台合格车都会被判失败。
"""

from __future__ import annotations

from core.verdict import CheckResult, Status, judge_range

from ._util import run_probe


def run(ctx) -> CheckResult:
    result = CheckResult(number=9, title="激光雷达", status=Status.PASS)
    cfg = ctx.baseline.get("radar", {})
    freq = ctx.baseline.get("frequency", {}).get("radar_hz", {})

    data = run_probe(ctx, "lidar_probe", "/tianracer/scan", "5", timeout=120)
    if not data or not data.get("ok"):
        result.status = Status.FAIL
        result.summary = "雷达无数据（话题 /tianracer/scan 未发布）"
        return result

    low = float(freq.get("min", 28.0))
    high = float(freq.get("max", 32.0))
    hz = data.get("hz")
    result.add("频率", f"{hz:.2f}" if hz else "未测到", "Hz",
               expected=f"{low}–{high}")
    hz_status = judge_range(hz, low, high, warn_margin=1.0)

    points = data.get("points")
    expected_points = cfg.get("expected_points", 1440)
    result.add("点数/帧", points, "点", expected=str(expected_points))
    point_status = (Status.PASS if points == expected_points
                    else (Status.WARN if points else Status.SKIP))

    angle_min = data.get("angle_min_deg")
    angle_max = data.get("angle_max_deg")
    tol = float(cfg.get("angle_tolerance_deg", 1.0))
    result.add("角度范围", f"{angle_min}° ~ {angle_max}°",
               expected=f"{cfg.get('angle_min_deg')}° ~ {cfg.get('angle_max_deg')}°")
    angle_status = Status.PASS
    if None in (angle_min, angle_max):
        angle_status = Status.SKIP
    elif abs(angle_min - cfg.get("angle_min_deg", -180)) > tol or \
            abs(angle_max - cfg.get("angle_max_deg", 180)) > tol:
        angle_status = Status.WARN

    valid_ratio = data.get("valid_ratio")
    min_ratio = float(cfg.get("min_valid_ratio", 0.60))
    result.add("有效点占比", f"{valid_ratio:.1%}" if valid_ratio is not None else "?",
               expected=f"≥ {min_ratio:.0%}",
               note="后方无数据是 LakiBeam1 物理视场限制，属正常")
    ratio_status = judge_range(valid_ratio, min_ratio, 1.0)

    observed = data.get("observed_max_range")
    if observed is not None:
        result.add("实测最远", f"{observed:.2f}", "m")

    sectors = data.get("sectors", {})
    if sectors:
        result.add("扇形点数（前/后/左/右）",
                   f"{sectors.get('front')}/{sectors.get('rear')}/"
                   f"{sectors.get('left')}/{sectors.get('right')}",
                   note="后方为 0 属正常（物理视场 270°）")

    statuses = [hz_status, point_status, angle_status, ratio_status]
    if any(s is Status.FAIL for s in statuses):
        result.status = Status.FAIL
        if hz_status is Status.FAIL:
            result.summary = f"频率异常 {hz} Hz（期望 {low}–{high}）"
        elif ratio_status is Status.FAIL:
            result.summary = f"有效点仅 {valid_ratio:.0%}，低于 {min_ratio:.0%}"
        else:
            result.summary = "雷达数据异常"
    elif any(s is Status.WARN for s in statuses):
        result.status = Status.WARN
        result.summary = "雷达数据偏离预期，需关注"
    else:
        result.summary = (f"{hz:.1f} Hz，{points} 点，有效 {valid_ratio:.0%}，"
                          f"最远 {observed:.1f} m" if None not in (hz, points, valid_ratio, observed)
                          else "雷达正常")

    result.detail = (
        "LakiBeam1 自报扫描范围 45~315°，折算约 ±135°。\n"
        "angle_min/angle_max 元数据写 ±180° 但后方取值范围为 inf/nan，属正常。\n"
        "不能用「360° 全覆盖、四扇区都有点」作为判定标准。"
    )
    return result
