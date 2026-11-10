"""7. 源码一致性。

回答三个问题：

1. 车上每个仓库在哪个分支、哪个提交
2. 有没有未提交改动（影响的是「买家拿到的版本」，不是车能不能跑）
3. 相对远端是**滞后**还是**超前**

第 3 点是 2026-11-10 补的。在此之前本项只查「有没有未提交改动」，
却把结论写成「N 个仓库均与远端一致」—— 根本没跟远端比过。
实车随即露馅：tianbot_core 的 origin/dev 已经从 7ce54f3 走到 c47921d，
报告仍说一致。

怎么比：用 `git ls-remote` 直接问远端 HEAD 的 SHA，**不执行 `git fetch`**。
fetch 本身是安全的（只写 .git，不动工作区，也不影响运行中的节点），
但它会改动被测对象的本地引用状态。检测工具不该在检测过程中改变被测物，
所以这里选零副作用的那条路。

## 三类失败必须分开（2026-11-10 Round 14 审查）

| 现象 | 含义 | 本项怎么记 |
| --- | --- | --- |
| 命令返回非 0 | remote 坏 / SSH 不通 / 无凭据 | 无法比对（WARN） |
| 命令成功但输出为空 | 远端没有这个分支 | **未推送（FAIL）** |
| 输出有内容但解析不出 SHA | 无权限或地址异常 | 无法比对（WARN） |

「未推送」判 FAIL 而不是 WARN，因为它比「落后」更严重：落后还能 fetch 补回来，
未推送的分支**换一台车就复现不出来**。出厂检查要保证可复现，所以这是硬伤。

## 为什么所有路径都经 shquote

`branch` / `remote` 来自车上 git 的输出，而 git 的分支名规则允许 `;` `$` `&` `|`
和反引号（只禁空格与 `?*[~^\\`）。裸拼进 shell 就是注入点。
`path` 来自 profile 的 `ros.workspace`，同样可能含空格。
"""

from __future__ import annotations

import re

from core.ssh import shquote
from core.verdict import CheckResult, Status

# 仓库清单与工作空间来自产品配置档（profile.yaml 的 ros.repos / ros.workspace）

# 远端关系。返回值第三项是「读不到」的原因（能判定时为空）。
_SYNC = "同步"
_AHEAD = "领先"
_BEHIND = "落后"
_UNFETCHED = "落后（本地未 fetch）"
_DIVERGED = "分叉"
_UNKNOWN = "读取失败"

# ls-remote 的 SHA 行：<40 位十六进制>\t<ref>
_SHA_LINE = re.compile(r"^([0-9a-f]{7,40})\s+\S")
# 输出里出现这些词说明远端真的报错了（不是「分支不存在」）
_FAIL_HINTS = ("fatal:", "error:", "permission denied",
               "could not read", "authentication failed")


def _remote_path(path: str) -> str:
    """远程路径的 shell 写法：`~/` 换成 $HOME，其余部分转义。

    直接 shquote('~/ws') 会得到 '~/ws' —— 单引号里的 ~ 不展开，cd 直接失败。
    所以拆开处理：$HOME 保持可展开，后面的分段再转义。
    """
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shquote(path[2:])
    return shquote(path)


def _probe_script(path: str) -> str:
    """本地状态探测脚本：一次 SSH 拿全，减少往返。

    不用 str.format / f-string 拼这段：`@{u}` 里的花括号在两种语法里都会被
    当占位符，而 workspace 路径里出现 `{` 会让 .format() 直接抛 ValueError。
    普通字符串拼接没有这两个坑。
    """
    return (
        "cd " + _remote_path(path) + " 2>/dev/null || exit 3\n"
        'echo "BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"\n'
        'echo "HEAD=$(git rev-parse HEAD 2>/dev/null)"\n'
        "echo \"SHORT=$(git log -1 --format='%h %ad' --date=short 2>/dev/null)\"\n"
        "echo \"UP=$(git rev-parse --abbrev-ref '@{u}' 2>/dev/null)\"\n"
        "echo '---STATUS---'\n"
        "git status --short 2>/dev/null\n"
    )


def _parse_local(out: str):
    """把 _probe_script 的输出拆成 (字段字典, 未提交改动列表)。"""
    tags, _, status = (out or "").partition("---STATUS---")
    vals = {}
    for line in tags.splitlines():
        key, sep, val = line.partition("=")
        if sep:
            vals[key.strip()] = val.strip()
    changes = [ln for ln in status.splitlines() if ln.strip()]
    return vals, changes


def _int(text: str) -> int:
    try:
        return int((text or "").strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0


def _remote_url(remote_runner, path: str, remote: str) -> str:
    """取 remote 地址。读不到返回空串。"""
    res = remote_runner.run(
        "cd " + _remote_path(path) + " 2>/dev/null && "
        f"git remote get-url {shquote(remote)} 2>&1",
        timeout=15, record=False,
    )
    if not res.ok:
        return ""
    return (res.stdout or "").strip()


def _remote_head(remote_runner, path: str, remote: str, branch: str):
    """问远端该分支的提交 SHA。

    返回 (sha, kind, msg)，kind ∈ ok / unreachable / unpushed / unparsable。
    只读，不 fetch。
    """
    if not branch:
        return "", "unreachable", "分支名未知"
    res = remote_runner.run(
        "cd " + _remote_path(path) + " 2>/dev/null && "
        f"git ls-remote --heads {shquote(remote)} "
        f"refs/heads/{shquote(branch)} 2>&1",
        timeout=20, record=False,
    )
    text = (res.stdout or "").strip()
    if not res.ok:
        # 命令本身失败：remote 地址坏、SSH 不通、无凭据
        return "", "unreachable", "远端不可达"

    # 不能只看第一行：git 会把 `warning: redirecting to ...` 打到 stderr，
    # 而这里用了 2>&1（否则 SSH 报错就看不到）。gh-proxy 镜像地址必带这条
    # warning，实车上就因此把「未推送分支」误报成「输出无法解析」。
    for line in text.splitlines():
        m = _SHA_LINE.match(line.strip())
        if m:
            return m.group(1), "ok", ""

    # 没有任何 SHA 行。git 退出码为 0，所以不是「命令失败」，
    # 而是远端确实没有这个分支。
    low = text.lower()
    if any(hint in low for hint in _FAIL_HINTS):
        # 退出码正常但输出里带 fatal —— 极少见，当错误处理而非「未推送」
        return "", "unparsable", "远端返回错误信息（可能无权限或地址异常）"
    return "", "unpushed", f"远端没有 {branch} 分支"


def _has_commit(remote_runner, path: str, sha: str):
    """本地是否已有该提交对象。True / False / None（读不到）。

    用 HAVE/MISS 显式标记而不是依赖 `cat-file -e` 的退出码：
    后者对「对象不存在」和「SSH 中途断了」都返回非 0，无法区分。
    分不清的后果是把传输故障报成「本地未 fetch」，维修人员会去 fetch
    而不是去查网络。
    """
    res = remote_runner.run(
        "cd " + _remote_path(path) + " 2>/dev/null || exit 3\n"
        "if git cat-file -e " + shquote(sha) + "^{commit} 2>/dev/null; "
        "then echo HAVE; else echo MISS; fi",
        timeout=15, record=False,
    )
    text = (res.stdout or "").strip()
    if "HAVE" in text:
        return True
    if "MISS" in text:
        return False
    return None


def _ancestor(remote_runner, path: str, older: str, newer: str):
    """older 是否为 newer 的祖先。True / False / None（读不到）。

    同样显式回传退出码：merge-base 用 1 表示「不是祖先」，128 表示出错。
    """
    res = remote_runner.run(
        "cd " + _remote_path(path) + " 2>/dev/null || exit 3\n"
        f"git merge-base --is-ancestor {shquote(older)} {shquote(newer)} "
        "2>/dev/null\n"
        "echo RC=$?",
        timeout=15, record=False,
    )
    m = re.search(r"RC=(\d+)", res.stdout or "")
    if not m:
        return None
    rc = int(m.group(1))
    if rc == 0:
        return True
    if rc == 1:
        return False
    return None


def _count(remote_runner, path: str, rng: str) -> int:
    res = remote_runner.run(
        "cd " + _remote_path(path) + " 2>/dev/null && "
        f"git rev-list --count {rng}",
        timeout=15, record=False,
    )
    return _int(res.stdout)


def _compare(remote_runner, path: str, head: str, remote_sha: str):
    """判断本地相对远端的关系。返回 (关系, 相差提交数)。"""
    if not remote_sha or not head:
        return _UNKNOWN, 0
    if remote_sha == head:
        return _SYNC, 0

    has = _has_commit(remote_runner, path, remote_sha)
    if has is None:
        return _UNKNOWN, 0
    if not has:
        return _UNFETCHED, 0

    anc = _ancestor(remote_runner, path, remote_sha, head)
    if anc is None:
        return _UNKNOWN, 0
    if anc:
        return _AHEAD, _count(remote_runner, path, f"{remote_sha}..{head}")

    anc = _ancestor(remote_runner, path, head, remote_sha)
    if anc is None:
        return _UNKNOWN, 0
    if anc:
        return _BEHIND, _count(remote_runner, path, f"{head}..{remote_sha}")

    # 两边各有对方没有的提交
    return _DIVERGED, _count(remote_runner, path, f"{remote_sha}..{head}")


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
    problems = []      # 读不到 git 信息
    unresolved = []    # 读到了，但远端比不了
    unpushed = []      # 本地分支远端不存在 —— 出厂风险，判 FAIL
    drift = []         # 与远端不一致

    for repo in repos:
        path = f"{ws}/src/{repo}"
        probe = r.run(_probe_script(path), timeout=25)
        if not probe.ok or "---STATUS---" not in (probe.stdout or ""):
            result.add(repo, "不可读", note="git 信息读取失败")
            problems.append(repo)
            continue

        vals, changes = _parse_local(probe.stdout)
        branch = vals.get("BRANCH") or "?"
        head = vals.get("HEAD") or ""
        short = vals.get("SHORT") or "?"
        upstream = vals.get("UP") or ""

        dirty_total += len(changes)
        result.add(f"{repo} 分支/HEAD", f"{branch} @ {short}")
        result.add(f"{repo} 未提交", len(changes), "项", expected="0")

        # upstream 形如 origin/dev；没有 upstream 就按同名远端分支猜
        if "/" in upstream:
            remote, _, ubranch = upstream.partition("/")
        else:
            remote, ubranch = "origin", (branch if branch != "?" else "")

        # 先取地址再问远端。顺序不能反：三斜杠地址必然让 ls-remote 失败，
        # 若在失败分支直接 continue，这条唯一的根因诊断就永远打不出来
        # （2026-11-10 Round 14 审查指出，旧顺序把它变成了死代码）。
        url = _remote_url(r, path, remote)
        bad_url = bool(url and re.search(r"https:///", url))
        if bad_url:
            result.add(f"{repo} remote 地址", url,
                       note="格式异常（三斜杠），ls-remote/fetch 必然失败")

        remote_sha, kind, msg = _remote_head(r, path, remote, ubranch)
        if kind == "unpushed":
            result.add(f"{repo} 相对远端", "未推送", expected="已推送",
                       note=f"{remote}/{ubranch}：{msg}。换一台车无法复现这批改动")
            unpushed.append(f"{repo}（{branch}）")
            continue
        if kind != "ok":
            note = f"{remote}/{ubranch or '?'}：{msg}"
            if bad_url:
                note += "；地址三斜杠，需先修 remote URL"
            result.add(f"{repo} 远端比对", "无法比对", note=note)
            unresolved.append(repo)
            continue

        rel, cnt = _compare(r, path, head, remote_sha)
        if rel == _UNKNOWN:
            # 远端 SHA 拿到了，但本地读不出关系（对象缺失之外的原因：
            # SSH 半途断开、仓库损坏）。归到无法比对，不算 drift。
            result.add(f"{repo} 相对远端", "无法比对",
                       note=(f"{remote}/{ubranch} @ {remote_sha[:8]} 已取到，"
                             f"但本地 git 读不出先后关系"))
            unresolved.append(repo)
            continue
        shown = f"{rel}" + (f" {cnt} 个提交" if cnt else "")
        result.add(f"{repo} 相对远端", shown, expected=_SYNC,
                   note=(f"比对 {remote}/{ubranch} @ {remote_sha[:8]}"
                         f"（ls-remote，未改动本地）"))
        if rel != _SYNC:
            drift.append(f"{repo} {rel}" + (f" {cnt} 个提交" if cnt else ""))

    # ---- 汇总：四类问题一次性列出，不用 early return ----
    # 旧实现每类问题各自 return，导致「A 仓库读不到」会盖掉「B 仓库落后」。
    parts = []
    if unpushed:
        parts.append("本地分支未推送：" + "、".join(unpushed))
    if drift:
        parts.append("与远端不一致：" + "、".join(drift))
    if unresolved:
        parts.append("无法比对：" + "、".join(unresolved))
    if problems:
        parts.append("无法读取：" + "、".join(problems))
    if dirty_total:
        parts.append(f"{dirty_total} 项未提交改动")

    if unpushed:
        # 出厂的东西必须可复现。落后还能 fetch 补回来，未推送的分支不能。
        result.status = Status.FAIL
        result.summary = "；".join(parts)
        result.detail = (
            "「本地分支未推送」判失败：这批提交换一台车就复现不出来，\n"
            "出厂后维修点无法还原车上到底装了什么。\n"
            "处理方式：把该分支推到远端（或改到已推送的分支上），再复测本项。\n\n"
            "其余说明：「落后」= 车上代码比远端旧，可能缺修复；\n"
            "「领先」= 含远端没有的提交；「无法比对」= 远端不可达或无权限，\n"
            "与车况无关，需修 remote 配置。\n\n"
            "本项用 git ls-remote 只读比对，未执行 fetch/pull，\n"
            "车上的工作区与正在运行的节点都不受影响。"
        )
        return result

    if parts:
        result.status = Status.WARN
        result.summary = "；".join(parts)
        if drift or unresolved or problems:
            result.detail = (
                "「落后」= 车上代码比远端旧，可能缺修复；「领先」= 含远端没有的"
                "提交（例如本项目的串口采集补丁），这类提交若未推送，换一台车就"
                "复现不出来。\n"
                "「无法比对」= 远端不可达或无权限，与车况无关。\n"
                "未提交改动不影响车能不能跑，影响的是买家拿到的代码版本。\n"
                "本项用 git ls-remote 只读比对，没有执行 fetch/pull，"
                "车上的工作区与正在运行的节点都不受影响。"
            )
        return result

    result.summary = f"{len(repos)} 个仓库均与远端分支一致，无未提交改动"
    return result
