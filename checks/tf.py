"""13. TF 树。"""

from __future__ import annotations

from core.remote import ros
from core.verdict import CheckResult, Status

from ._util import run_probe


def run(ctx) -> CheckResult:
    result = CheckResult(number=13, title="TF 树", status=Status.PASS)
    required = ctx.baseline.get("tf", {}).get("required_frames", [])

    data = run_probe(ctx, "tf_probe", "3", timeout=60)
    if not data or not data.get("ok"):
        result.status = Status.FAIL
        result.summary = "无法读取 TF 树（/tf 无数据）"
        return result

    frames = data.get("frames", {})
    result.add("frame 总数", data.get("frame_count", 0))

    missing = [f for f in required if f not in frames]
    # TF 树必然有恰好一个根节点（map / world / odom），它没有父节点是正常的。
    # 只有出现 2 个以上无父 frame 才是真的断链（审查建议，虽实测本车
    # all_frames_as_yaml 不列根节点，但加固后可避版本差异导致的误报）。
    orphans = [f for f, parent in frames.items() if not parent]
    real_orphans = orphans if len(orphans) > 1 else []

    if required:
        result.add("必需 frame", f"{len(required) - len(missing)}/{len(required)}",
                   expected=f"{len(required)}/{len(required)}")

    # 关键变换校验（父节点不一定作为独立 frame 出现）
    transforms = ctx.baseline.get("tf", {}).get("required_transforms", [])
    missing_tf = []
    for source, target in transforms:
        check = ctx.remote.run(
            ros(f"timeout 6 rosrun tf tf_echo {source} {target} 2>&1 | head -3"),
            timeout=20, record=False,
        ).stdout
        ok = "Translation" in check
        result.add(f"{source} → {target}", "可查" if ok else "缺失")
        if not ok:
            missing_tf.append(f"{source}→{target}")

    if missing:
        result.status = Status.FAIL
        result.summary = "缺少必需 frame：" + "、".join(missing)
        result.detail = (
            "常见原因：只启动了 tianracer_core.launch 而没有启动 "
            "tianracer_description.launch，导致 robot_state_publisher 未运行，\n"
            "base_link / base_footprint 等 frame 不存在。\n"
            "正确做法：roslaunch tianracer_bringup tianracer_bringup.launch"
        )
    elif missing_tf:
        result.status = Status.FAIL
        result.summary = "关键变换缺失：" + "、".join(missing_tf)
    elif real_orphans:
        result.status = Status.WARN
        result.summary = f"{len(real_orphans)} 个 frame 无父节点（可能是断链）"
    else:
        for frame in ("tianracer/laser", "tianracer/base_footprint", "tianracer/imu_link"):
            if frame in frames:
                result.add(frame, f"← {frames[frame]}")
        result.summary = f"{len(frames)} 个 frame 全部连通，必需项齐全"

    return result
