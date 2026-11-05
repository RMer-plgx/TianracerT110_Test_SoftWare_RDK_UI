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

# 「哪些变量已经在会话内被覆盖」不再写死 —— 多产品后每个产品的变量名都不同，
# 硬编码的集合永远抓不到新产品的变量，报告会谎称「不在自动修正白名单」。
# 现在直接由产品的 ros.env_overrides（即 ros_env_prefix 实际 export 的键）决定。
def _overridden_names(ctx) -> set:
    return set(ctx.profile.ros.env_overrides)


def run(ctx) -> CheckResult:
    result = CheckResult(number=4, title="环境变量", status=Status.PASS)
    baseline = ctx.baseline

    # 产品没有声明任何环境变量前缀 → 本项不适用，直接跳过。
    # 不能因此报错：很多产品本来就没有这套配置。
    if not ctx.profile.env_prefix:
        result.status = Status.SKIP
        result.summary = "本产品未配置环境变量（profile 里没有 env_prefix），该项不适用"
        return result

    expected = baseline.get("envvars", {})
    known = baseline.get("envvars_known_deviations", {})

    actual = read_env_vars(ctx.remote, ctx.profile)
    if not actual:
        result.status = Status.FAIL
        result.summary = "读取环境变量失败（source devel/setup.bash 可能出错）"
        return result

    result.add("检测到的 TIAN* 变量", len(actual), "项",
               expected=f"{len(expected)} 项")

    overridden = _overridden_names(ctx)
    corrected = []       # 真正被 ros_env_prefix 覆盖的
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
            if name in overridden:
                corrected.append(name)
                result.add(name, got, expected=str(want), note="已临时修正")
            else:
                not_fixed.append(name)
                result.add(name, got, expected=str(want),
                           note="不符，但不在自动修正白名单")

    # 已知偏差 / 不符项的判定
    if unknown_bad:
        # 程序已在会话内把能修的修好了，车实际按正确参数运行，
        # 但配置本身还是错的（重装/换机器会复现）。如实报 WARN。
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
        hook = ctx.profile.ros.env_hook_file
        lines.append("未修改车辆任何文件。根本修复方式：" +
                     (f"把 {hook} 的默认值改成正确值后重新编译。"
                      if hook else
                      "把环境变量默认值改成正确值后重新编译。"))
    if not_fixed:
        if lines:
            lines.append("")
        lines.append("以下变量与环境期望不符，但**不在自动修正白名单**内，"
                     "程序未修改它们，可能影响后续检测结果：")
        lines.extend(f"  - {name}" for name in not_fixed)

    # 变量分组说明由产品自己声明（profile.yaml 的 env_note）。
    # 每个产品的变量清单不同，写死在代码里对新品就是错的指引。
    note = getattr(ctx.profile, "env_note", "")
    if note:
        if lines:
            lines.append("")
        lines.extend(note.splitlines())

    if lines:
        result.detail = "\n".join(lines)
    return result
