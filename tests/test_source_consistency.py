"""源码一致性（第 7 项）的远端比对。

2026-11-10 之前的实现只查「有没有未提交改动」，却把结论写成
「N 个仓库均与远端一致」——根本没跟远端比过。实车立刻露馅：
tianbot_core 的 origin/dev 已经走到 c47921d，车上还停在旧提交，
报告依然写「一致」。

Round 14 审查又指出三处：
- 三斜杠 remote 的诊断被 `continue` 吞成了死代码（而这台车恰好就是三斜杠）
- SSH 故障被归因成「落后（本地未 fetch）」，维修人员会去 fetch 而不是查网络
- 一个仓库读不到时，其它仓库的 drift 被 early return 吞掉

这组测试锁住：

1. 真的去比远端（ls-remote），同步/滞后/超前/分叉/未推送/不可达各自区分
2. 远端没这个分支 → **FAIL**（换台车复现不出来，出厂风险）
3. SSH 故障不能伪装成「本地未 fetch」
4. 多仓库时各类问题都要出现在 summary 里，不互相覆盖
5. 全过程只读 —— 不允许出现 fetch / pull / checkout / reset
6. 路径与分支名经 shquote，且 `~/` 必须能展开
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from checks import source as source_check  # noqa: E402
from core.verdict import Status  # noqa: E402
from tests import _fixtures  # noqa: E402

HEAD_A = "a" * 40
HEAD_B = "b" * 40


class _Result:
    def __init__(self, stdout="", ok=True):
        self.stdout = stdout
        self.stderr = ""
        self.ok = ok


class _ScriptedRemote:
    """按子串匹配命令，按声明顺序依次消费，记录所有命令供「只读」断言。"""

    def __init__(self, table):
        self.table = list(table)   # [(子串, _Result)]
        self.commands = []
        self._used = set()

    def run(self, cmd, timeout=30, record=True):
        self.commands.append(cmd)
        for i, (needle, result) in enumerate(self.table):
            if i in self._used:
                continue
            if needle in cmd:
                self._used.add(i)
                return result
        return _Result("", ok=False)


class _Ctx:
    """最小 profile 替身：只有本项用到的 ros.repos / ros.workspace。"""

    def __init__(self, remote, repos, workspace="~/ws"):
        self.profile = type("P", (), {
            "ros": type("R", (), {"repos": list(repos),
                                  "workspace": workspace})(),
        })()
        self.remote = remote


_LOCAL_OK = (
    "BRANCH=feature\n"
    "HEAD=" + HEAD_A + "\n"
    "SHORT=aaaaaaa 2026-11-10\n"
    "UP=origin/feature\n"
    "---STATUS---\n"
)
_LOCAL_DIRTY = _LOCAL_OK + " M CMakeLists.txt\n?? notes.txt\n"
_URL_OK = "https://github.com/tianbot/x.git\n"
_URL_BAD = "https:///gh-proxy.com/https://github.com/tianbot/x.git\n"

_PROBE = "git status --short"      # _probe_script 里唯一可辨识的片段


def _run(table, local=_LOCAL_OK, repos=("tianracer",), workspace="~/ws"):
    remote = _ScriptedRemote([(_PROBE, _Result(local))] + list(table))
    return source_check.run(_Ctx(remote, repos=repos, workspace=workspace)), remote


def _rows(result):
    """把 measurements 摊平成 {名称: 值} 便于断言。"""
    return {m.name: m.value for m in result.measurements}


class TestRemoteComparison:
    def test_in_sync_passes(self):
        res, _ = _run([
            ("git remote get-url", _Result(_URL_OK)),
            ("git ls-remote", _Result(HEAD_A + "\trefs/heads/feature\n")),
        ])
        assert res.status is Status.PASS
        assert "一致" in res.summary

    def test_behind_is_warn(self):
        res, _ = _run([
            ("git remote get-url", _Result(_URL_OK)),
            ("git ls-remote", _Result(HEAD_B + "\trefs/heads/feature\n")),
            ("cat-file", _Result("HAVE\n")),
            ("merge-base", _Result("RC=1\n")),      # b 不是 a 的祖先
            ("merge-base", _Result("RC=0\n")),      # a 是 b 的祖先 → 落后
            ("rev-list", _Result("7\n")),
        ])
        assert res.status is Status.WARN
        assert "落后" in res.summary
        assert "7 个提交" in res.summary

    def test_ahead_is_warn(self):
        res, _ = _run([
            ("git remote get-url", _Result(_URL_OK)),
            ("git ls-remote", _Result(HEAD_B + "\trefs/heads/feature\n")),
            ("cat-file", _Result("HAVE\n")),
            ("merge-base", _Result("RC=0\n")),      # b 是 a 的祖先 → 领先
            ("rev-list", _Result("3\n")),
        ])
        assert res.status is Status.WARN
        assert "领先" in res.summary

    def test_diverged(self):
        res, _ = _run([
            ("git remote get-url", _Result(_URL_OK)),
            ("git ls-remote", _Result(HEAD_B + "\trefs/heads/feature\n")),
            ("cat-file", _Result("HAVE\n")),
            ("merge-base", _Result("RC=1\n")),
            ("merge-base", _Result("RC=1\n")),
            ("rev-list", _Result("2\n")),
        ])
        assert res.status is Status.WARN
        assert "分叉" in res.summary

    def test_unfetched_remote_is_honest(self):
        """远端提交本地没有对象（cat-file 明确回 MISS）→ 不许猜成同步。"""
        res, _ = _run([
            ("git remote get-url", _Result(_URL_OK)),
            ("git ls-remote", _Result(HEAD_B + "\trefs/heads/feature\n")),
            ("cat-file", _Result("MISS\n")),
        ])
        assert res.status is Status.WARN
        assert "未 fetch" in res.summary

    def test_ssh_failure_is_not_reported_as_unfetched(self):
        """R14-2：命令本身失败（SSH 断）不能报成「本地未 fetch」。

        真实语义：cat-file 对存在的对象返回 0（无输出），对不存在的返回非 0。
        两者都无法从「没有 MISS」推出来，所以必须用 HAVE/MISS 显式标记。
        """
        res, _ = _run([
            ("git remote get-url", _Result(_URL_OK)),
            ("git ls-remote", _Result(HEAD_B + "\trefs/heads/feature\n")),
            ("cat-file", _Result("", ok=False)),    # 命令根本没跑起来
        ])
        assert res.status is Status.WARN
        assert "无法比对" in res.summary
        assert "未 fetch" not in res.summary

    def test_unreachable_remote_does_not_crash(self):
        res, _ = _run([
            ("git remote get-url", _Result(_URL_OK)),
            ("git ls-remote", _Result("fatal: Could not read from remote\n",
                                      ok=False)),
        ])
        assert res.status is Status.WARN
        assert "无法比对" in res.summary


class TestTripleSlashDiagnosis:
    """R14-1：三斜杠地址必然让 ls-remote 失败，诊断不能被 continue 吞掉。"""

    def test_bad_url_reported_alongside_unreachable(self):
        res, _ = _run([
            ("git remote get-url", _Result(_URL_BAD)),
            ("git ls-remote", _Result("fatal: repository not found\n", ok=False)),
        ])
        rows = _rows(res)
        # 两条都要有：既说清「比不了」，也说清「为什么比不了」
        assert any("remote" in k for k in rows), f"缺少 remote 诊断：{rows}"
        assert any("无法比对" in str(v) for v in rows.values()), rows
        assert res.status is Status.WARN

    def test_bad_url_note_mentions_three_slash(self):
        res, _ = _run([
            ("git remote get-url", _Result(_URL_BAD)),
            ("git ls-remote", _Result("fatal: repository not found\n", ok=False)),
        ])
        notes = " ".join(m.note for m in res.measurements)
        assert "三斜杠" in notes

    def test_url_fetched_even_when_ls_remote_fails(self):
        """顺序保证：URL 必须在 ls-remote 之前取。"""
        _, remote = _run([
            ("git remote get-url", _Result(_URL_BAD)),
            ("git ls-remote", _Result("fatal\n", ok=False)),
        ])
        joined = "\n".join(remote.commands)
        assert joined.index("remote get-url") < joined.index("ls-remote")


class TestUnpushedBranch:
    """远端没有这个分支 → FAIL。换台车复现不出来，出厂风险。"""

    def test_unpushed_branch_fails(self):
        res, _ = _run([
            ("git remote get-url", _Result(_URL_OK)),
            ("git ls-remote", _Result("")),          # 成功但无输出
        ])
        assert res.status is Status.FAIL
        assert "未推送" in res.summary
        rows = _rows(res)
        assert any("未推送" in str(v) for v in rows.values()), rows

    def test_gh_proxy_warning_line_does_not_hide_unpushed(self):
        """实车回归：gh-proxy 镜像会打 `warning: redirecting to ...`。

        旧解析只看第一行，于是把 warning 当成「无法解析」，把真正的
        「分支未推送」误报成 WARN。实车 2026-11-10 即为此。
        """
        res, _ = _run([
            ("git remote get-url", _Result(_URL_BAD)),
            ("git ls-remote",
             _Result("warning: redirecting to https://gh-proxy.com/x/\n")),
        ])
        assert res.status is Status.FAIL
        assert "未推送" in res.summary
        assert "无法解析" not in res.summary

    def test_sha_line_after_warning_is_parsed(self):
        """warning 在前、SHA 在后时仍要读到 SHA。"""
        res, _ = _run([
            ("git remote get-url", _Result(_URL_BAD)),
            ("git ls-remote",
             _Result("warning: redirecting to https://gh-proxy.com/x/\n"
                     + HEAD_A + "\trefs/heads/feature\n")),
        ])
        assert res.status is Status.PASS

    def test_unpushed_summary_names_the_repo(self):
        res, _ = _run(
            [
                ("git remote get-url", _Result(_URL_OK)),
                ("git ls-remote", _Result("")),
            ],
            repos=["tianbot_core"],
        )
        assert "tianbot_core" in res.summary

    def test_unpushed_detail_explains_why_fatal(self):
        res, _ = _run([
            ("git remote get-url", _Result(_URL_OK)),
            ("git ls-remote", _Result("")),
        ])
        assert "复现" in res.detail


class TestReadOnly:
    def test_never_mutates_the_repo(self):
        _, remote = _run([
            ("git remote get-url", _Result(_URL_OK)),
            ("git ls-remote", _Result(HEAD_B + "\trefs/heads/feature\n")),
            ("cat-file", _Result("HAVE\n")),
            ("merge-base", _Result("RC=0\n")),
            ("rev-list", _Result("0\n")),
        ])
        joined = " ".join(remote.commands)
        # "git merge " 带空格，避免误伤只读的 git merge-base
        for forbidden in ("git fetch", "git pull", "git checkout",
                          "git reset", "git switch", "git merge "):
            assert forbidden not in joined, (
                f"检测过程中出现了会改变被测状态的命令：{forbidden}")

    def test_uses_ls_remote_not_fetch(self):
        _, remote = _run([
            ("git remote get-url", _Result(_URL_OK)),
            ("git ls-remote", _Result(HEAD_A + "\trefs/heads/feature\n")),
        ])
        assert any("git ls-remote" in c for c in remote.commands)


class TestQuoting:
    """R14-3：路径与分支名必须转义，`~/` 必须能展开。"""

    def test_tilde_is_expanded_not_quoted(self):
        _, remote = _run([
            ("git remote get-url", _Result(_URL_OK)),
            ("git ls-remote", _Result(HEAD_A + "\trefs/heads/feature\n")),
        ])
        probe = next(c for c in remote.commands if _PROBE in c)
        # 不能出现 '~/...' —— 单引号里的 ~ 不展开，cd 会失败
        assert "'~/" not in probe, probe
        assert '"$HOME"/' in probe, probe

    def test_workspace_with_space_is_quoted(self):
        _, remote = _run(
            [
                ("git remote get-url", _Result(_URL_OK)),
                ("git ls-remote", _Result(HEAD_A + "\trefs/heads/feature\n")),
            ],
            workspace="/opt/my ws",
        )
        probe = next(c for c in remote.commands if _PROBE in c)
        # 整条路径被一对单引号包住，空格因此不再分割词
        assert "cd '/opt/my ws/src/tianracer'" in probe, probe

    def test_workspace_with_brace_does_not_break(self):
        """`{` 若走 .format() 会抛 ValueError，改用拼接后应当正常。"""
        res, _ = _run(
            [
                ("git remote get-url", _Result(_URL_OK)),
                ("git ls-remote", _Result(HEAD_A + "\trefs/heads/feature\n")),
            ],
            workspace="/opt/we{ird}",
        )
        assert res.status is Status.PASS


class TestMultipleRepos:
    """R14-4：一个仓库出问题不能吞掉另一个仓库的结论。"""

    def test_unreadable_repo_does_not_hide_drift(self):
        remote = _ScriptedRemote([
            # 仓库 a：probe 失败
            (_PROBE, _Result("", ok=False)),
            # 仓库 b：probe 正常，落后
            (_PROBE, _Result(_LOCAL_OK)),
            ("git remote get-url", _Result(_URL_OK)),
            ("git ls-remote", _Result(HEAD_B + "\trefs/heads/feature\n")),
            ("cat-file", _Result("HAVE\n")),
            ("merge-base", _Result("RC=1\n")),
            ("merge-base", _Result("RC=0\n")),
            ("rev-list", _Result("4\n")),
        ])
        res = source_check.run(_Ctx(remote, repos=["a", "b"]))
        assert res.status is Status.WARN
        assert "无法读取" in res.summary, res.summary
        assert "与远端不一致" in res.summary, res.summary

    def test_unpushed_wins_over_warn(self):
        """同时有「无法比对」与「未推送」时，判 FAIL 而不是 WARN。"""
        remote = _ScriptedRemote([
            (_PROBE, _Result(_LOCAL_OK)),
            (_PROBE, _Result(_LOCAL_OK)),
            ("git remote get-url", _Result(_URL_OK)),
            ("git ls-remote", _Result("fatal\n", ok=False)),   # a 不可达
            ("git ls-remote", _Result("")),                    # b 未推送
        ])
        res = source_check.run(_Ctx(remote, repos=["a", "b"]))
        assert res.status is Status.FAIL
        assert "未推送" in res.summary
        assert "无法比对" in res.summary


class TestDirtyAndEdges:
    def test_dirty_but_in_sync(self):
        res, _ = _run(
            [
                ("git remote get-url", _Result(_URL_OK)),
                ("git ls-remote", _Result(HEAD_A + "\trefs/heads/feature\n")),
            ],
            local=_LOCAL_DIRTY,
        )
        assert res.status is Status.WARN
        assert "未提交" in res.summary

    def test_no_repos_skips(self):
        res = source_check.run(_Ctx(_ScriptedRemote([]), repos=[]))
        assert res.status is Status.SKIP


class TestYawToleranceTightened:
    """用户 2026-11-10 要求：姿态误差可控制在 3° 内。"""

    def test_baseline_tolerance_is_three_degrees(self):
        spin = _fixtures.baseline()["motion"]["spin"]
        assert spin["yaw_tolerance_deg"] == 3
        assert spin["yaw_warn_deg"] <= 10

    def test_warn_is_looser_than_tolerance(self):
        spin = _fixtures.baseline()["motion"]["spin"]
        assert spin["yaw_warn_deg"] > spin["yaw_tolerance_deg"]


class TestManualButtonRemoved:
    """「人工检测项」按钮点击无反应，用户要求直接删掉（2026-11-10）。"""

    def test_ui_module_has_no_manual_button(self):
        src = (Path(__file__).resolve().parent.parent
               / "ui" / "main_window.py").read_text(encoding="utf-8")
        assert 'QPushButton("人工检测项")' not in src
        assert "open_manual_check" not in src
        assert "manual_btn" not in src

    def test_report_still_keeps_manual_section(self):
        """删的是 GUI 入口，不是报告里的复核表——那是判定的载体。"""
        tpl = (Path(__file__).resolve().parent.parent
               / "report" / "html.j2").read_text(encoding="utf-8")
        assert 'id="manual"' in tpl
