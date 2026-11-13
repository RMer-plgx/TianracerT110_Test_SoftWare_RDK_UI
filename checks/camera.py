"""11. USB 摄像头。"""

from __future__ import annotations

import time

from core.verdict import CheckResult, Status, judge_all, judge_range

from ._util import echo_once, measure_hz, parse_float


def run(ctx) -> CheckResult:
    result = CheckResult(number=14, title="USB 摄像头", status=Status.PASS)
    cfg = ctx.baseline.get("camera", {})
    freq = ctx.baseline.get("frequency", {}).get("camera_hz", {})
    r = ctx.remote

    devices = r.run("v4l2-ctl --list-devices 2>&1", timeout=25)
    result.add("设备枚举", devices.stdout.splitlines()[0].strip()
               if devices.stdout.strip() else "未发现设备")

    link = r.run("readlink -f /dev/tianbot_usb_cam 2>&1", timeout=15)
    result.add("软链指向", link.stdout.strip() or "不存在")

    topic = ctx.profile.ros.topic("camera")
    hz = measure_hz(ctx, topic, seconds=8)

    # 相机由 webrtc 桥推流，bringup 刚起来时可能还没开始发帧。
    # 实车验证：刚启动时读到 None，等一段时间后稳定 30 Hz。
    # 所以第一次拿不到就重试，不直接判失败。
    if hz is None:
        ctx.say("       相机暂无数据，等待 10 秒后重试…")
        time.sleep(10)
        hz = measure_hz(ctx, topic, seconds=8)

    low = float(freq.get("min", 20.0))
    high = float(freq.get("max", 40.0))
    result.add("频率", f"{hz:.2f}" if hz is not None else "无数据", "Hz",
               expected=f"{low}–{high}",
               note="允许偶发卡顿；首次无数据会等待并重试一次")
    hz_status = judge_range(hz, low, high)

    if hz is None:
        # 根本拿不到帧，不必再去读分辨率等其他字段
        result.status = Status.FAIL
        result.summary = f"图像话题无数据（{topic}），重试后仍未收到"
        result.detail = (
            "排查：ls -l /dev/video0 看设备是否在；"
            "sudo lsof /dev/video0 看是否被 ffmpeg 独占。"
        )
        return result

    width = parse_float(echo_once(ctx, topic, "width") or "")
    height = parse_float(echo_once(ctx, topic, "height") or "")
    encoding = echo_once(ctx, topic, "encoding") or ""

    exp_w = cfg.get("expected_width", 640)
    exp_h = cfg.get("expected_height", 480)
    exp_enc = cfg.get("expected_encoding", "bgr8")

    result.add("分辨率", f"{int(width) if width else '?'}×{int(height) if height else '?'}",
               expected=f"{exp_w}×{exp_h}")
    result.add("编码", encoding or "?", expected=exp_enc)

    fmt_status = Status.PASS
    if width is None or height is None:
        fmt_status = Status.SKIP
    elif int(width) != exp_w or int(height) != exp_h:
        fmt_status = Status.WARN
    if encoding and encoding != exp_enc:
        fmt_status = Status.WARN

    statuses = [hz_status, fmt_status]
    # 统一收口，避免「子项 SKIP 但初始 PASS 没被覆盖」（审查发现）
    result.status = judge_all(statuses)

    if result.status is Status.FAIL:
        result.summary = "图像参数或频率不合格"
    elif result.status is Status.WARN:
        result.summary = "图像参数与期望不符或频率偏低"
    elif result.status is Status.SKIP:
        missing = []
        if width is None or height is None:
            missing.append("分辨率")
        if not encoding:
            missing.append("编码")
        result.summary = "未取得数据：" + "、".join(missing or ["图像参数"])
    else:
        result.summary = f"{int(width)}×{int(height)} {encoding} @ {hz:.1f} Hz"
    return result
