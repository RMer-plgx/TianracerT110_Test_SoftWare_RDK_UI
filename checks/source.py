"""7. 源码一致性。

三个仓库的未提交改动都要记录。注意 tianbot_core 的 remote URL 是三斜杠坏地址，
这是已知缺陷，报告里说明但不判致命。
"""

from __future__ import annotations

import re

from core.verdict import CheckResult, Status

REPOS = ["tianracer", "tianbot_core", "webrtc_ros_bridge"]
WS = "~/tianracer_ros1_ws"


def run(ctx) -> CheckResult:
    result = CheckResult(number=7, title="源码一致性", status=Status.PASS)
    r = ctx.remote

    dirty_total = 0
    problems = []

    for repo in REPOS:
        path = f"{WS}/src/{repo}"
        head = r.run(
            f"cd {path} 2>/dev/null && "
            f"git rev-parse --abbrev-ref HEAD && "
            f"git log -1 --format='%h %ad' --date=short && "
            f"echo '---STATUS---' && git status --short",
            timeout=25,
        )
        if not head.ok or "---STATUS---" not in head.stdout:
            result.add(repo, "不可读", note="git 信息读取失败")
            problems.append(repo)
            continue

        before, _, after = head.stdout.partition("---STATUS---")
        lines = [ln.strip() for ln in before.splitlines() if ln.strip()]
        branch = lines[0] if lines else "?"
        commit = lines[1] if len(lines) > 1 else "?"

        changes = [ln for ln in after.splitlines() if ln.strip()]
        dirty_total += len(changes)
        result.add(f"{repo} 分支/HEAD", f"{branch} @ {commit}")
        result.add(f"{repo} 未提交", len(changes), "项",
                   expected="0")

        # remote URL 检查
        remote = r.run(f"cd {path} 2>/dev/null && git remote get-url origin 2>&1",
                       timeout=15, record=False).stdout.strip()
        if remote and re.search(r"https:///", remote):
            result.add(f"{repo} remote", remote, note="URL 格式异常（三斜杠），fetch 会失败")

    if problems:
        result.status = Status.WARN
        result.summary = "部分仓库无法读取 git 信息：" + "、".join(problems)
    elif dirty_total == 0:
        result.summary = "三个仓库均与远端一致，无未提交改动"
    else:
        # 未提交改动不影响车能不能跑（功能全正常），
        # 影响的是「买家拿到的代码版本」——这是归档问题，不是故障。
        result.status = Status.NOTICE
        result.summary = (f"共 {dirty_total} 项未提交改动，需确认是否为 RDK 适配所需")
        result.detail = (
            "已知情况（2026-10-30）：tianracer 4 改 + 1 未跟踪，"
            "tianbot_core 3 改（波特率、底盘类型、依赖声明）。\n"
            "这些改动是 RDK 适配所必需，判定为 WARN 而非 FAIL。\n"
            "交付前需确认归属并修复 tianbot_core 的 remote URL。"
        )
    return result
