"""13. TF 树。"""

from __future__ import annotations

from core.remote import ros
from core.verdict import CheckResult, Status

from ._util import run_probe

# TF 树合法的根节点名称（去命名空间后比较）。
# 它们没有父节点是正常的，不应报断链。
LEGAL_ROOT_NAMES = {"map", "world", "odom"}


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

    # 无父 frame 分两类：
    #   - 合法根（map / world / odom 等）→ 正常，TF 树必然有一个根
    #   - 其他 → 真断链
    # 不能用「数量≤1 就豁免」——单个真断链会被吞掉，
    # 而且报告会写「全部连通」与事实相反（Round 2 审查发现）。
    roots, dangling = [], []
    for frame, parent in frames.items():
        if parent:
            continue
        short = frame.split("/")[-1]
        if short in LEGAL_ROOT_NAMES:
            roots.append(frame)
        else:
            dangling.append(frame)

    if required:
        result.add("必需 frame", f"{len(required) - len(missing)}/{len(required)}",
                   expected=f"{len(required)}/{len(required)}")

    # 关键变换校验（父节点不一定作为独立 frame 出现）
    transforms = ctx.baseline.get("tf", {}).get("required_transforms", [])
    missing_tf = []
    for source, target in transforms:
        check = ctx.remote.run(
            ros(f"timeout 6 rosrun tf tf_echo {source} {target} 2>&1 | head -3",
                       ctx.profile),
            timeout=20, record=False,
        ).stdout
        ok = "Translation" in check
        result.add(f"{source} → {target}", "可查" if ok else "缺失")
        if not ok:
            missing_tf.append(f"{source}→{target}")

    if missing:
        result.status = Status.FAIL
        result.summary = "缺少必需 frame：" + "、".join(missing)
        pkg = ctx.profile.ros.launch_package
        launch = ctx.profile.ros.launch_file
        result.detail = (
            "常见原因：只启动了底盘驱动，没有启动带 robot_state_publisher 的 "
            "description/urdf 部分，导致 base_link / base_footprint 等 frame 不存在。\n"
            f"正确做法：roslaunch {pkg} {launch}"
        )
    elif missing_tf:
        result.status = Status.FAIL
        result.summary = "关键变换缺失：" + "、".join(missing_tf)
    elif dangling:
        result.status = Status.WARN
        result.summary = "疑似断链：" + "、".join(dangling[:3]) + " 无父节点"
    else:
        # frame 列表来自产品配置，不写死 —— 不同产品的 frame 名不同，
        # 写死的后果是新产品报告里这一块静默消失，不报错也不提示。
        for frame in ctx.profile.ros.display_frames:
            if frame in frames:
                result.add(frame, f"← {frames[frame]}")
        if roots:
            result.add("根节点", "、".join(roots))
        result.summary = f"{len(frames)} 个 frame 全部连通，必需项齐全"

    return result
