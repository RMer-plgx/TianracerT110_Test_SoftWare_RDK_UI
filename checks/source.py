"""7. 源码一致性。

三个仓库的未提交改动都要记录。注意 tianbot_core 的 remote URL 是三斜杠坏地址，
这是已知缺陷，报告里说明但不判致命。
"""

from __future__ import annotations

import re

from core.verdict import CheckResult, Status

# 仓库清单与工作空间来自产品配置档（profile.yaml 的 ros.repos / ros.workspace）


def run(ctx) -> CheckResult:
    result = CheckResult(number=7, title="源码一致性", status=Status.PASS)
    r = ctx.remote

    repos = ctx.profile.ros.repos
    ws = ctx.profile.ros.workspace
    if not repos:
        result.status = Status.SKIP
        result.summary = "产品配置未声明源码仓库（ros.repos 为空），本项跳过"
        return result

    dirty_total = 0
    problems = []

    for repo in repos:
        path = f"{ws}/src/{repo}"
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
        result.summary = f"{len(repos)} 个仓库均与远端一致，无未提交改动"
    else:
        # 未提交改动不影响车能不能跑，影响的是买家拿到的代码版本。
        # 这是归档问题，需人工确认后接受。
        result.status = Status.WARN
        result.summary = (f"共 {dirty_total} 项未提交改动，需确认是否为适配所需")
        result.detail = (
            "未提交改动不影响车能不能跑，影响的是买家拿到的代码版本。\n"
            "这属于归档问题，需人工确认后接受。\n"
            "交付前应把适配改动提交到仓库，或修复异常的 remote URL。"
        )
    return result
