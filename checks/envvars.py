"""4. 环境变量。

判据来源：**车辆 `~/.bashrc` 里的显式声明**，不是非交互会话里的进程环境。

## 为什么改（2026-11-13，ADR-0012）

旧实现跑的是 `source devel/setup.bash; env | grep TIANRACER`。
这条路径读到的是 env-hooks 的默认值，而不是车辆配置 —— 因为
`~/.bashrc` 开头有：

    case $- in
        *i*) ;;
          *) return;;
    esac

非交互 shell 在第 20 行就 `return` 了，后面的 `export` 不执行。
实测：

    bash -ic 'echo $TIANRACER_BASE_PORT'   → /dev/ttyUSB0      （交互，正确）
    bash -c  'source setup.bash; echo ...' → /dev/tianbot_base  （非交互，错）

车本身没问题 —— 用户交互登录时一切正常。所以「车辆标准」应该看
`.bashrc` 写了什么。

## 判定规则

| 情况 | 结论 |
| --- | --- |
| `.bashrc` 声明了，且等于期望值 | PASS |
| `.bashrc` 声明了，但不等于期望值 | WARN「车辆声明 X，期望 Y」 |
| `.bashrc` 未声明 | 只记录，不判定（说明由 env-hooks 提供默认值） |

## 车还能跑的原因

`profile.yaml` 的 `env_overrides` 在每次 SSH 会话里 export 正确值，
把 env-hooks 的错误默认值盖掉了。但那只是「每次运行临时擦屁股」，
不是「车被修好」—— 所以本项仍要如实报告车辆配置。
"""

from __future__ import annotations

from core.remote import read_bashrc_exports, read_env_vars
from core.verdict import CheckResult, Status


def _overridden_names(ctx) -> set:
    return set(ctx.profile.ros.env_overrides)


def run(ctx) -> CheckResult:
    result = CheckResult(number=4, title="环境变量", status=Status.PASS)
    baseline = ctx.baseline

    if not ctx.profile.env_prefix:
        result.status = Status.SKIP
        result.summary = "本产品未配置环境变量（profile 里没有 env_prefix），该项不适用"
        return result

    expected = baseline.get("envvars", {})
    known = baseline.get("envvars_known_deviations", {})
    prefix = ctx.profile.env_prefix

    # ---- 主判据：车辆 .bashrc 的声明 ----
    # 前缀要从 baseline 的键名推导，不能只用 env_prefix：
    # T110 的变量既有 TIANRACER_* 也有 TIANBOT_NAME（后者会被漏掉）。
    prefixes = {prefix} | {n.split("_")[0] for n in expected if "_" in n}
    declared = read_bashrc_exports(ctx.remote, sorted(prefixes))
    result.add("车辆声明的变量", len(declared), "项",
               expected=f"{len(expected)} 项",
               note="读自 ~/.bashrc 的 export 行（不受非交互 shell 影响）")

    # ---- 参考：非交互会话实际拿到的值 ----
    # 用来说明「为什么车能跑但配置有问题」，不参与判定。
    actual = read_env_vars(ctx.remote, ctx.profile)

    mismatched = []      # .bashrc 声明了但不符
    missing = []         # .bashrc 未声明
    matched = []

    for name, want in expected.items():
        want_s = str(want)
        got = declared.get(name)
        if got is None:
            missing.append((name, want_s, actual.get(name)))
            continue
        if got == want_s:
            matched.append(name)
            result.add(name, got, expected=want_s)
        else:
            mismatched.append((name, got, want_s))
            result.add(name, got, expected=want_s,
                       note="车辆 .bashrc 声明值与标准不符")

    # ---- 判定 ----
    if mismatched:
        result.status = Status.WARN
        names = [n for n, _, _ in mismatched]
        result.summary = (
            f"{len(mismatched)} 项与车辆标准不符（.bashrc 声明值 ≠ 期望）："
            + "、".join(names[:4])
            + (" 等" if len(names) > 4 else "")
        )
    elif declared and missing:
        # 声明了部分变量，其余没声明 —— 记录但不报 WARN：
        # 那些变量由 env-hooks 提供默认值，车辆没声明不代表错。
        result.summary = (
            f"车辆声明了 {len(matched)} 项且全部符合；"
            f"其余 {len(missing)} 项由 env-hooks 提供默认值"
        )
    elif not declared:
        result.status = Status.WARN
        result.summary = (
            f"车辆 ~/.bashrc 里没有声明任何 {prefix}* 变量；"
            f"环境全部来自 env-hooks 默认值"
        )
    else:
        result.summary = f"{len(matched)} 项环境变量与车辆声明一致"

    # ---- detail：把「车辆声明 vs 运行时实际」并列，便于排查 ----
    lines = []

    if mismatched:
        lines.append("以下变量在 ~/.bashrc 里声明了，但与标准不符：")
        for name, got, want in mismatched:
            lines.append(f"  - {name}: 车辆声明 {got}，期望 {want}")
        lines.append("")

    if missing:
        lines.append("以下变量在 ~/.bashrc 里未声明，由 env-hooks 提供默认值：")
        for name, want, got in missing:
            shown = got if got is not None else "未设置"
            lines.append(f"  - {name}: 运行时 {shown}（期望 {want}）")
        lines.append("")

    # 已知偏差说明
    for name, info in (known or {}).items():
        reason = (info or {}).get("reason", "")
        if reason and name in declared:
            lines.append(f"已知偏差 {name}：{reason}")
    if lines and lines[-1] != "":
        lines.append("")

    # 为什么会存在「声明值 ≠ 运行时值」
    overridden = _overridden_names(ctx)
    if overridden:
        lines.append(
            "本次测试在子 shell 内临时覆盖了以下变量（仅本次会话生效，"
            "用于保证能打开底盘）："
        )
        lines.extend(f"  - {n}" for n in sorted(overridden))
        lines.append("")
        lines.append("未修改车辆任何文件。")
        lines.append("")

    hook = ctx.profile.ros.env_hook_file
    lines.append("判据来源：~/.bashrc 的 export 行。")
    lines.append(
        "之所以不读运行时环境：~/.bashrc 第 18-20 行在非交互 shell 里会提前 "
        "return，检测软件的 SSH 会话读不到那些 export，"
        "拿到的是 env-hooks 的默认值。"
    )
    if hook:
        lines.append(f"env-hooks 默认值来自：{hook}")

    note = getattr(ctx.profile, "env_note", "")
    if note:
        lines.append("")
        lines.extend(note.splitlines())

    result.detail = "\n".join(lines)
    return result
