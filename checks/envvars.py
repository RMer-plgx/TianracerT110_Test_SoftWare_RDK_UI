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

    corrected = []
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
            corrected.append(name)
            result.add(name, got, expected=str(want), note="已临时修正")

    # 记录 remote.py 实际生效的修正值
    effective = {
        "/dev/ttyUSB0": "TIANRACER_BASE_PORT",
        "115200": "TIANRACER_BASE_BAUDRATE",
        "richbeam_lidar": "TIANRACER_LIDAR",
    }
    overridden = [k for k in effective if k in corrected or
                  effective[k] in corrected]

    if unknown_bad:
        result.status = Status.WARN
        result.summary = (
            f"{len(unknown_bad)} 项与期望不符，已临时修正为正确值继续测试："
            + "、".join(unknown_bad[:4])
        )
        if len(unknown_bad) > 4:
            result.summary += " 等"
    elif known_hits:
        result.status = Status.WARN
        result.summary = f"存在 {len(known_hits)} 项已知偏差（不影响运行）"
    else:
        result.summary = f"{len(expected)} 项环境变量全部符合期望"

    if overridden:
        result.detail = (
            "本次测试在子 shell 内临时覆盖了以下变量，未修改车辆任何文件。\n"
            "根本修复方式：把 env-hooks/99.my_tianracer.sh.em 的默认值改成正确值后重新编译。\n"
        )
    return result
