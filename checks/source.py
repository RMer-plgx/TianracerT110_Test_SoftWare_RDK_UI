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
_SHA_LINE = re.compile(r"^([0-9a-f]{7,40})\s+(\S+)")
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


def _remote_url(remote_runner, path: str, remote: str):
    """取 remote 地址。返回 (url, 状态)。

    状态分三种，因为含义完全不同：
      ok      取到了
      missing 这个 remote 名根本没配（车上用的可能是别的名字）
      failed  命令本身没跑成（仓库读不到、git 异常）

    旧版把后两种都返回空串，于是「没配 origin」被报成「远端不可达」——
    用户会去查网络，而实际该做的是看 `git remote -v`。
    """
    res = remote_runner.run(
        "cd " + _remote_path(path) + " 2>/dev/null || exit 3\n"
        f"git remote get-url {shquote(remote)} 2>&1\n"
        "echo RC=$?",
        timeout=15, record=False,
    )
    text = (res.stdout or "").strip()
    m = re.search(r"RC=(\d+)", text)
    body = re.sub(r"RC=\d+$", "", text).strip()
    if not m:
        return "", "failed"
    rc = int(m.group(1))
    if rc == 0 and body:
        return body.splitlines()[0].strip(), "ok"
    if rc != 0 and not body:
        # get-url 对不存在的 remote 报「No such remote」并返回 2；
        # 但 2>&1 后 body 里会有这句话，所以空 body + 非 0 才是「完全没有」。
        return "", "failed"
    if "no such remote" in body.lower():
        return "", "missing"
    return "", "failed"


def _known_head(head: str) -> str:
    """把本地 HEAD 限制成 SHA 才能拼进 rev-list 的范围。"""
    return head if re.fullmatch(r"[0-9a-f]{7,40}", head or "") else ""


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
    # 每行还要核对 ref 名：refspec 正常情况下已由 git 过滤，但万一没生效
    # （远端返回了全部分支），拿第一个 SHA 当目标分支就会静默得出错误的
    # 「领先/落后」结论。宁可不认，也不能认错。
    want = "refs/heads/" + branch
    for line in text.splitlines():
        m = _SHA_LINE.match(line.strip())
        if m and m.group(2) == want:
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

    用 HAVE/MISS 显式标记而不是依赖 `cat-file -e` 的退出码，
    并把「缺失」与「出错」分开：
      cat-file 对存在的对象返回 0
      对不存在的对象返回 128
    其它退出码（git 本身不存在、仓库损坏）不是「缺失」，是「读不到」。
    分不清的后果是把传输故障报成「本地未 fetch」，维修人员会去 fetch
    而不是去查网络。
    """
    res = remote_runner.run(
        "cd " + _remote_path(path) + " 2>/dev/null || exit 3\n"
        "git cat-file -e " + shquote(sha) + "^{commit} 2>/dev/null\n"
        "echo RC=$?",
        timeout=15, record=False,
    )
    m = re.search(r"RC=(\d+)", res.stdout or "")
    if not m:
        return None
    rc = int(m.group(1))
    if rc == 0:
        return True
    if rc == 128:
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


def _short_shas(remote_runner, path: str, rng: str) -> set:
    """列出一个 commit 范围里的短 sha（7 位，小写）。

    用于 known_divergences 比对：光有「领先 2 个」不够，
    还得知道是哪 2 个，才能判断是不是已确认等价的那几个。
    """
    res = remote_runner.run(
        "cd " + _remote_path(path) + " 2>/dev/null && "
        f"git rev-list --abbrev-commit --abbrev=7 {rng} 2>/dev/null",
        timeout=20, record=False,
    )
    return {ln.strip().lower() for ln in (res.stdout or "").splitlines() if ln.strip()}


def _compare(remote_runner, path: str, head: str, remote_sha: str):
    """判断本地相对远端的关系。返回 (关系, 相差提交数)。

    head 与 remote_sha 都必须先过 SHA 白名单才能拼进 rev-list 的范围：
    `git rev-list --count A..B` 的两个参数在这里是裸拼的（不能转义，
    引号会把范围变成单个参数），所以只能靠格式校验来保证安全。
    """
    head = _known_head(head)
    remote_sha = _known_head(remote_sha)
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


def _check_known_divergence(remote_runner, path: str, head: str,
                            remote_sha: str, rel: str, declared):
    """检查这次分叉是否就是配置里声明过的「等价分叉」。

    命中条件（全部满足才返回说明文字，否则返回 None）：
      1. 配置里声明了这个仓库
      2. 本地领先的提交 ⊆ local_commits
      3. 远端领先的提交 ⊆ remote_commits

    第 2/3 条是关键：任何一边多出新提交就不算命中 ——
    那说明真的变了（上游有新功能、或本地有新改动），必须报出来。

    `rel` 是 _compare 的结论（领先/落后/分叉），只用来说明今天的实际关系。
    """
    if not declared:
        return None
    allowed_local = declared.get("local_commits") or set()
    allowed_remote = declared.get("remote_commits") or set()
    if not allowed_local and not allowed_remote:
        return None

    # 本地领先的（远端有、本地没合并的部分不看这里）
    local_only = _short_shas(remote_runner, path, f"{remote_sha}..{head}")
    remote_only = _short_shas(remote_runner, path, f"{head}..{remote_sha}")

    extra_local = local_only - allowed_local
    extra_remote = remote_only - allowed_remote
    if extra_local or extra_remote:
        # 有清单外的提交，不认
        return None

    reason = declared.get("reason") or "已确认内容等价"
    when = declared.get("verified_at") or ""
    parts = [reason]
    if when:
        parts.append(f"核对于 {when}")
    if local_only:
        parts.append("本地领先 " + ", ".join(sorted(local_only)))
    if remote_only:
        parts.append("远端领先 " + ", ".join(sorted(remote_only)))
    parts.append("两边清单外的提交出现时会重新报 WARN")
    return "；".join(parts)


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
    unpushed = []      # 本地分支远端不存在，且未在配置里声明 —— 判 FAIL
    declared = []      # 本地分支未推送，但配置里已声明是交付镜像自带
    declared_equiv = []  # 未推送/分叉，但配置里声明了两边内容等价
    accepted = []        # 已知可接受（remote 不可达 / 现场适配的未提交改动）
    accept_unreachable = set(getattr(ctx.profile.ros, "accept_unreachable", []) or [])
    accept_dirty = bool(getattr(ctx.profile.ros, "accept_dirty", False))
    drift = []         # 与远端不一致

    allowed = getattr(ctx.profile.ros, "allowed_local_branches", {}) or {}

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

        # detached HEAD：`git rev-parse --abbrev-ref HEAD` 成功返回字面量
        # "HEAD"（不是失败），而上游查询会报错被 2>/dev/null 吞掉。
        # 若照常当成分支名去猜 origin/HEAD，必然查不到 —— 会被判成
        # 「本地分支未推送」→ FAIL，而车上根本没有名为 HEAD 的分支。
        # 现场用 `git checkout <tag>` 核对版本就会进这个状态，
        # 这是正常动作，不能把整车判成不合格。
        if branch == "HEAD":
            result.add(f"{repo} 分支/HEAD", f"detached @ {short}",
                       note="不在任何分支上（可能 checkout 到了 tag 或某个提交），"
                            "没有可对比的远端分支")
            result.add(f"{repo} 未提交", len(changes), "项", expected="0")
            unresolved.append(f"{repo}（detached）")
            continue

        result.add(f"{repo} 分支/HEAD", f"{branch} @ {short}")
        result.add(f"{repo} 未提交", len(changes), "项", expected="0")

        # upstream 形如 origin/dev；没有 upstream 就按同名远端分支猜。
        # 猜出来的关系要在 note 里标明「无 upstream」，否则读者会以为
        # 这个结论是 git 自己算的。
        if "/" in upstream:
            remote, _, ubranch = upstream.partition("/")
            guessed = False
        else:
            remote, ubranch = "origin", (branch if branch != "?" else "")
            guessed = True

        # known_divergences 可以指定比对基准分支。
        # 车上是本地改造分支（feat/xxx），评测等价应该看上游主干。
        _kd = getattr(ctx.profile.ros, "known_divergences", {}).get(repo) or {}
        _cw = _kd.get("compare_with") or ""
        if _cw:
            ubranch = _cw
            guessed = False

        # 先取地址再问远端。顺序不能反：比对失败时若还没取过地址，
        # 报告就只剩「远端不可达」，看不出 remote 到底配成了什么
        # （2026-11-10 Round 14 审查指出，旧顺序把这条诊断变成了死代码）。
        #
        # 注意：三斜杠**不是**「必然失败」。2026-11-11 实测：
        #   git ls-remote --heads "https:///gh-proxy.com/https://..."
        #   warning: redirecting to https://gh-proxy.com/https://.../
        #   8912ccec...  refs/heads/amp          exit=0
        # git 会把三斜杠归一化成两斜杠并重定向，能正常工作。
        # 上一版写的「必然失败」是错误断言，且与自己报告的「未推送」
        # （要求 ls-remote 成功才能判）自相矛盾。现在降级为「写法不规范，
        # 通常可用」，只在真的比对失败时提示去检查。
        url, url_state = _remote_url(r, path, remote)
        bad_url = bool(url and re.search(r"https:///", url))
        if bad_url:
            result.add(f"{repo} remote 地址", url,
                       note="地址是三斜杠写法（不规范）；git 通常会自动归一化"
                            "并重定向，实测可用。若下方比对失败再考虑修 URL")
        elif url_state == "missing":
            # 「没配 origin」和「网络不通」是两件事：前者该去看 git remote -v，
            # 后者该去查网络。旧版把两者都报成「远端不可达」。
            result.add(f"{repo} remote 地址", f"没有名为 {remote} 的 remote",
                       note=f"车上实际配置的 remote 需用 git remote -v 查看；"
                            f"本项按 {remote} 比对，配置不符时无法得出结论")
            unresolved.append(f"{repo}（无 {remote} remote）")
            continue

        remote_sha, kind, msg = _remote_head(r, path, remote, ubranch)
        # 无 upstream 时分支名是猜的，「未推送」这个结论也建立在猜测上，
        # 必须写出来，否则读者会以为这是 git 自己算的。
        guess_hint = "；无 upstream，按同名分支比对" if guessed else ""
        if kind == "unpushed":
            if allowed.get(repo) == branch:
                # 已声明：交付镜像自带的改动，可复现的责任在仓库侧而不是这台车。
                result.add(f"{repo} 相对远端", "已声明的本地分支",
                           note=(f"{remote}/{ubranch} 不存在，但 profile 的 "
                                 f"allowed_local_branches 声明了它；"
                                 f"仍记录下来供人工核对{guess_hint}"))
                declared.append(f"{repo}（{branch}）")
            elif allowed.get(repo):
                result.add(f"{repo} 相对远端", "未推送", expected="已推送",
                           note=(f"{remote}/{ubranch}：{msg}。配置里声明的是 "
                                 f"`{allowed.get(repo)}`，与当前分支 `{branch}` "
                                 f"不符 —— 不生效{guess_hint}"))
                unpushed.append(f"{repo}（{branch}）")
            else:
                result.add(f"{repo} 相对远端", "未推送", expected="已推送",
                           note=(f"{remote}/{ubranch}：{msg}。"
                                 f"换一台车无法复现这批改动{guess_hint}"))
                unpushed.append(f"{repo}（{branch}）")
            continue
        if kind != "ok":
            note = f"{remote}/{ubranch or '?'}：{msg}" + guess_hint
            if bad_url:
                note += ("；地址三斜杠虽能被 git 归一化，但此处比对仍失败，"
                         "建议核对 git remote -v")
            if repo in accept_unreachable:
                result.add(f"{repo} 远端比对", "无法比对（已知可接受）", note=note)
                accepted.append(f"{repo}（远端不可达）")
            else:
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
        tail = "（ls-remote，未改动本地）"
        if guessed:
            tail = guess_hint + tail
        base_note = f"比对 {remote}/{ubranch} @ {remote_sha[:8]}{tail}"

        # 先看是不是已确认等价的分叉 —— 命中就只记一行，不重复
        known = None
        if rel != _SYNC:
            known = _check_known_divergence(
                r, path, head, remote_sha, rel,
                getattr(ctx.profile.ros, "known_divergences", {}).get(repo))

        if known:
            result.add(f"{repo} 相对远端", f"已确认等价分叉（{shown}）",
                       expected=_SYNC,
                       note=f"{known}。{base_note}")
            declared_equiv.append(
                f"{repo}（{rel} {cnt} 个提交）" if cnt else repo)
            continue

        result.add(f"{repo} 相对远端", shown, expected=_SYNC, note=base_note)
        if rel != _SYNC:
            drift.append(f"{repo} {rel}" + (f" {cnt} 个提交" if cnt else ""))

    # ---- 汇总：四类问题一次性列出，不用 early return ----
    # 旧实现每类问题各自 return，导致「A 仓库读不到」会盖掉「B 仓库落后」。
    # parts = 真正的问题（决定 WARN/FAIL）
    # notes = 已确认项（只作说明，不影响状态）
    parts = []
    notes = []

    if dirty_total:
        if accept_dirty:
            accepted.append(f"{dirty_total} 项未提交改动（现场适配）")
        else:
            parts.append(f"{dirty_total} 项未提交改动")

    if unpushed:
        parts.append("本地分支未推送：" + "、".join(unpushed))
    if declared:
        # 按 ADR-0007：声明过但仍是「未推送」，判 WARN（可见、可确认）
        parts.append("已声明的本地分支：" + "、".join(declared))
    if drift:
        parts.append("与远端不一致：" + "、".join(drift))
    if unresolved:
        parts.append("无法比对：" + "、".join(unresolved))
    if problems:
        parts.append("无法读取：" + "、".join(problems))

    # 这两类是「已确认没问题」，只作说明
    if declared_equiv:
        notes.append("已确认等价分叉：" + "、".join(declared_equiv))
    if accepted:
        notes.append("已知可接受：" + "、".join(accepted))

    if unpushed:
        # 出厂的东西必须可复现。落后还能 fetch 补回来，未推送的分支不能。
        result.status = Status.FAIL
        result.summary = "；".join(parts)
        result.detail = (
            "「本地分支未推送」判失败：这批提交换一台车就复现不出来，\n"
            "出厂后维修点无法还原车上到底装了什么。\n"
            "处理方式（选一）：把该分支推到远端；或改到已推送的分支上；\n"
            "或如果它是交付镜像自带的车端改动，在 profile.yaml 的\n"
            "`ros.allowed_local_branches` 里声明（仓库名: 分支名），\n"
            "声明后本项降为可确认的告警。\n\n"
            "其余说明：「落后」= 车上代码比远端旧，可能缺修复；\n"
            "「领先」= 含远端没有的提交；「无法比对」= 远端不可达或无权限，\n"
            "与车况无关，需修 remote 配置。\n\n"
            "本项用 git ls-remote 只读比对，未执行 fetch/pull，\n"
            "车上的工作区与正在运行的节点都不受影响。"
        )
        return result

    # 只有「已确认项」而没有真正的问题 → PASS
    if not parts and notes:
        result.summary = "检查通过（" + "；".join(notes) + "）"
        detail = ["以下项已确认，仍如实记录供核对："]
        detail.extend(f"  - {n}" for n in notes)
        detail.append("")
        detail.append(
            "未隐藏任何信息。要恢复严格判定：删掉 profile.yaml 的 "
            "known_divergences / accept_unreachable / accept_dirty 即可。"
        )
        detail.append("")
        detail.append(
            "本项用 git ls-remote + git rev-list 只读比对，"
            "未执行 fetch/pull，工作区与运行中的节点都不受影响。"
        )
        result.detail = "\n".join(detail)
        return result

    if parts:
        result.status = Status.WARN
        result.summary = "；".join(parts)
        if drift or unresolved or problems or declared:
            result.detail = (
                "「已声明的本地分支」= 交付镜像自带的改动，已在 profile 里登记；\n"
                "它不可从远端复现，所以仍要人工看一眼，但不算失败。\n"
                "「落后」= 车上代码比远端旧，可能缺修复；「领先」= 含远端没有的"
                "提交；「无法比对」= 远端不可达、无权限或 detached HEAD，\n"
                "与车况无关。\n"
                "未提交改动不影响车能不能跑，影响的是买家拿到的代码版本。\n"
                "本项用 git ls-remote 只读比对，没有执行 fetch/pull，"
                "车上的工作区与正在运行的节点都不受影响。"
            )
        return result

    result.summary = f"{len(repos)} 个仓库均与远端分支一致，无未提交改动"
    return result
