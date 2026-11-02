"""4. 环境变量。

这一项是 2026-10-30 排查中最严重的坑：devel/setup.bash 里的错误默认值
（BASE_PORT=/dev/tianbot_base、BAUDRATE=460800、LIDAR=oradar_lidar）
会覆盖 ~/.bashrc 里的正确值，导致底盘完全打不开。

程序的处理方式：
- 检出偏差 → 记为 WARN（不是 FAIL，因为 remote.py 已经在会话内修正）
- 报告中明确标注「已临时修正」
- 绝不修改车辆文件
"""

from __future__ import annotations

from core.remote import diff_env_vars, read_env_vars
from core.verdict import CheckResult, Status, judge_envvar

# ROS_ENV_PREFIX 真正 export 的变量。
# 必须与 core/remote.py 的 ROS_ENV_PREFIX 保持一致——
# 那里多一个 export，这里就要多一项；否则报告会声称覆盖了实际没覆盖的变量。
OVERRIDDEN_BY_PREFIX = {
    "TIANRACER_BASE_PORT",
    "TIANRACER_BASE_BAUDRATE",
    "TIANRACER_LIDAR",
    "TIANRACER_LIDAR_IP",
    "TIANRACER_LIDAR_PORT",
    "TIANRACER_NAME",
}


def run(ctx) -> CheckResult:
    result = CheckResult(number=4, title="环境变量", status=Status.PASS)
    baseline = ctx.baseline

    expected = baseline.get("envvars", {})
    known = baseline.get("envvars_known_deviations", {})

    actual = read_env_vars(ctx.remote)
    if not actual:
        result.status = Status.FAIL
        result.summary = "读取环境变量失败（source devel/setup.bash 可能出错）"
        return result

    result.add("检测到的 TIAN* 变量", len(actual), "项",
               expected=f"{len(expected)} 项")

    corrected = []       # 真正被 ROS_ENV_PREFIX 覆盖的
    not_fixed = []       # 不符但不在覆盖白名单里
    known_hits = []
    unknown_bad = []

    for name, want in expected.items():
        got = actual.get(name)
        status = judge_envvar(got, str(want), known.get(name))
        if status is Status.PASS:
            continue
        if status is Status.WARN:
            known_hits.append(name)
            reason = (known.get(name) or {}).get("reason", "")
            result.add(name, got, expected=str(want),
                       note=f"已知偏差：{reason}")
        else:
            unknown_bad.append(name)
            # 只有 ROS_ENV_PREFIX 真的 export 了的才算「已修正」。
            # 否则 detail 会声称覆盖了实际没覆盖的变量（Round 2 审查发现）。
            if name in OVERRIDDEN_BY_PREFIX:
                corrected.append(name)
                result.add(name, got, expected=str(want), note="已临时修正")
            else:
                not_fixed.append(name)
                result.add(name, got, expected=str(want),
                           note="不符，但不在自动修正白名单")

    # 已知偏差 / 不符项的判定
    if unknown_bad:
        result.status = Status.WARN
        parts = []
        if corrected:
            parts.append(f"{len(corrected)} 项已临时修正")
        if not_fixed:
            parts.append(f"{len(not_fixed)} 项未修正")
        result.summary = (
            f"{len(unknown_bad)} 项与环境期望不符（{'，'.join(parts)}）："
            + "、".join(unknown_bad[:4])
        )
        if len(unknown_bad) > 4:
            result.summary += " 等"
    elif known_hits:
        result.status = Status.WARN
        result.summary = f"存在 {len(known_hits)} 项已知偏差（不影响运行）"
    else:
        result.summary = f"{len(expected)} 项环境变量全部符合期望"

    lines = []
    if corrected:
        lines.append("本次测试在子 shell 内临时覆盖了以下变量（仅本次会话生效）：")
        lines.extend(f"  - {name}" for name in corrected)
        lines.append("")
        lines.append("未修改车辆任何文件。根本修复方式：把 "
                     "env-hooks/99.my_tianracer.sh.em 的默认值改成正确值后重新编译。")
    if not_fixed:
        if lines:
            lines.append("")
        lines.append("以下变量与环境期望不符，但**不在自动修正白名单**内，"
                     "程序未修改它们，可能影响后续检测结果：")
        lines.extend(f"  - {name}" for name in not_fixed)

    # 这几行是为了回答「为什么这么多」——
    # TIANRACER_* 是 tianracer 的功能开关，每个模块一个变量，
    # 不是 17 个都必须手工配置的东西。
    if lines:
        lines.append("")
    lines.append("这 17 个变量不是 17 项手工配置，而是 tianracer 把每个功能模块"
                 "做成了一个开关（装什么填什么，没装的填 none）：")
    lines.append("  · 底盘通信：TIANRACER_BASE / _BASE_PORT / _BASE_BAUDRATE"
                 " —— 写错就完全打不开底盘，这三项最重要")
    lines.append("  · 雷达：TIANRACER_LIDAR / _LIDAR_IP / _LIDAR_PORT / _LIDAR_MODEL")
    lines.append("  · 功能模块（本车没装的填 none）：GPS / RGBD_CAMERA / RGBD_CAMERA_MODEL"
                 " / BRIDGE / VIDEO_DEVICE")
    lines.append("  · 手感与方向：TIANRACER_STEERING_DIRECTION / _THROTTLE_DIRECTION / _JOY_MODE")
    lines.append("  · 名称：TIANRACER_NAME / TIANBOT_NAME")

    if lines:
        result.detail = "\n".join(lines)
    return result
