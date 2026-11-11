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
    """最小 profile 替身：只有本项用到的字段。"""

    def __init__(self, remote, repos, workspace="~/ws", allowed=None):
        self.profile = type("P", (), {
            "ros": type("R", (), {"repos": list(repos),
                                  "workspace": workspace,
                                  "allowed_local_branches": dict(allowed or {})})(),
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
# `git rev-parse --abbrev-ref HEAD` 在 detached 状态下成功返回字面量 HEAD，
# 上游查询则报错被 2>/dev/null 吞掉。这正是 R15-1 的触发条件。
_LOCAL_DETACHED = (
    "BRANCH=HEAD\n"
    "HEAD=" + HEAD_A + "\n"
    "SHORT=aaaaaaa 2026-11-10\n"
    "UP=\n"
    "---STATUS---\n"
)
_URL_OK = "https://github.com/tianbot/x.git\nRC=0\n"
_URL_BAD = "https:///gh-proxy.com/https://github.com/tianbot/x.git\nRC=0\n"

_PROBE = "git status --short"      # _probe_script 里唯一可辨识的片段


def _run(table, local=_LOCAL_OK, repos=("tianracer",), workspace="~/ws",
         allowed=None):
    remote = _ScriptedRemote([(_PROBE, _Result(local))] + list(table))
    res = source_check.run(
        _Ctx(remote, repos=repos, workspace=workspace, allowed=allowed))
    return res, remote


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
            ("cat-file", _Result("RC=0\n")),
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
            ("cat-file", _Result("RC=0\n")),
            ("merge-base", _Result("RC=0\n")),      # b 是 a 的祖先 → 领先
            ("rev-list", _Result("3\n")),
        ])
        assert res.status is Status.WARN
        assert "领先" in res.summary

    def test_diverged(self):
        res, _ = _run([
            ("git remote get-url", _Result(_URL_OK)),
            ("git ls-remote", _Result(HEAD_B + "\trefs/heads/feature\n")),
            ("cat-file", _Result("RC=0\n")),
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
            ("cat-file", _Result("RC=128\n")),
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
            ("cat-file", _Result("RC=0\n")),
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
            ("cat-file", _Result("RC=0\n")),
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


class TestDetachedHead:
    """R15-1：detached HEAD 是正常动作，不能把整车判成不合格。

    `git rev-parse --abbrev-ref HEAD` 在 detached 状态下**成功**返回
    字面量 HEAD（不是失败），于是旧代码拿它当分支名去猜 origin/HEAD，
    查不到就判「本地分支未推送」→ FAIL。
    现场 `git checkout <tag>` 核对版本就会进这个状态。
    """

    def test_detached_head_is_warn_not_fail(self):
        res, _ = _run(
            [
                ("git remote get-url", _Result(_URL_OK)),
                ("git ls-remote", _Result("")),      # origin/HEAD 查不到
            ],
            local=_LOCAL_DETACHED,
        )
        assert res.status is Status.WARN, res.summary
        assert "未推送" not in res.summary, res.summary

    def test_detached_head_is_reported_as_detached(self):
        res, _ = _run(
            [
                ("git remote get-url", _Result(_URL_OK)),
                ("git ls-remote", _Result("")),
            ],
            local=_LOCAL_DETACHED,
        )
        rows = _rows(res)
        assert any("detached" in str(v) for v in rows.values()), rows

    def test_detached_head_does_not_query_origin_head(self):
        """不能拿字面量 HEAD 去当分支名查远端。"""
        _, remote = _run(
            [
                ("git remote get-url", _Result(_URL_OK)),
                ("git ls-remote", _Result("")),
            ],
            local=_LOCAL_DETACHED,
        )
        joined = " ".join(remote.commands)
        assert "refs/heads/'HEAD'" not in joined, joined

    def test_detached_head_still_counts_dirty(self):
        local = _LOCAL_DETACHED.replace("---STATUS---\n", "---STATUS---\n M x.txt\n")
        res, _ = _run([], local=local)
        assert "未提交" in res.summary


class TestAllowedLocalBranches:
    """R15-2：交付镜像自带的分支声明后走 WARN，未声明仍 FAIL。"""

    def test_declared_branch_is_warn(self):
        res, _ = _run(
            [
                ("git remote get-url", _Result(_URL_OK)),
                ("git ls-remote", _Result("")),
            ],
            allowed={"tianracer": "feature"},
        )
        assert res.status is Status.WARN, res.summary
        assert "已声明" in res.summary

    def test_declared_branch_still_visible(self):
        """声明了也要单列一行，不能藏起来。"""
        res, _ = _run(
            [
                ("git remote get-url", _Result(_URL_OK)),
                ("git ls-remote", _Result("")),
            ],
            allowed={"tianracer": "feature"},
        )
        rows = _rows(res)
        assert any("已声明" in str(v) for v in rows.values()), rows

    def test_undeclared_branch_still_fails(self):
        res, _ = _run(
            [
                ("git remote get-url", _Result(_URL_OK)),
                ("git ls-remote", _Result("")),
            ],
            allowed={"tianbot_core": "feat/x"},   # 声明的是别的仓库
        )
        assert res.status is Status.FAIL, res.summary

    def test_declared_but_different_branch_fails(self):
        """声明了仓库但分支名对不上，仍判失败，并说明不生效。"""
        res, _ = _run(
            [
                ("git remote get-url", _Result(_URL_OK)),
                ("git ls-remote", _Result("")),
            ],
            allowed={"tianracer": "other-branch"},
        )
        assert res.status is Status.FAIL, res.summary
        notes = " ".join(m.note for m in res.measurements)
        assert "不符" in notes, notes

    def test_declared_does_not_hide_real_unpushed(self):
        """多仓库：一个已声明、一个没声明 → 仍 FAIL。"""
        remote = _ScriptedRemote([
            (_PROBE, _Result(_LOCAL_OK)),
            (_PROBE, _Result(_LOCAL_OK)),
            ("git remote get-url", _Result(_URL_OK)),
            ("git ls-remote", _Result("")),
            ("git remote get-url", _Result(_URL_OK)),
            ("git ls-remote", _Result("")),
        ])
        res = source_check.run(_Ctx(
            remote, repos=["a", "b"], allowed={"a": "feature"}))
        assert res.status is Status.FAIL, res.summary
        assert "已声明" in res.summary and "未推送" in res.summary


class TestRemoteName:
    """R15-4：「没配 remote」与「远端不可达」要分开。"""

    def test_missing_remote_is_reported_as_missing(self):
        res, _ = _run([
            ("git remote get-url",
             _Result("error: No such remote 'origin'\nRC=2\n", ok=False)),
        ])
        assert res.status is Status.WARN
        assert "没有名为" in res.summary or "remote" in res.summary

    def test_missing_remote_does_not_query_ls_remote(self):
        _, remote = _run([
            ("git remote get-url",
             _Result("error: No such remote 'origin'\nRC=2\n", ok=False)),
        ])
        assert not any("ls-remote" in c for c in remote.commands)


class TestNoUpstream:
    """无 upstream 时按同名分支猜，必须在 note 里标明。"""

    def test_guess_disclosed_on_unpushed(self):
        """猜出来的分支名判定「未推送」，也必须标明是猜的。"""
        local = _LOCAL_OK.replace("UP=origin/feature\n", "UP=\n")
        res, _ = _run(
            [
                ("git remote get-url", _Result(_URL_OK)),
                ("git ls-remote", _Result("")),
            ],
            local=local,
        )
        assert res.status is Status.FAIL
        notes = " ".join(m.note for m in res.measurements)
        assert "无 upstream" in notes, notes

    def test_guessed_upstream_is_disclosed(self):
        local = _LOCAL_OK.replace("UP=origin/feature\n", "UP=\n")
        res, _ = _run(
            [
                ("git remote get-url", _Result(_URL_OK)),
                ("git ls-remote", _Result(HEAD_A + "\trefs/heads/feature\n")),
            ],
            local=local,
        )
        notes = " ".join(m.note for m in res.measurements)
        assert "无 upstream" in notes, notes


class TestRealVehicleShape:
    """端到端：用 2026-11-10 实车抓到的真实输出格式复刻三个仓库。

    这个场景比单个用例更接近现场，它同时覆盖：
      - tianracer 走 Gitee，正常同步
      - tianbot_core 走 gh-proxy 三斜杠地址 + warning 行 + 本地分支 + 已声明
      - webrtc_ros_bridge 走 SSH，无 host key
    """

    _SCRIPTS = {
        "tianracer": {
            "probe": ("BRANCH=formula-mini\nHEAD=" + HEAD_A + "\n"
                      "SHORT=ff48b1e 2026-01-07\nUP=origin/formula-mini\n"
                      "---STATUS---\n M a.txt\n"),
            "url": "https://gitee.com/tianbot/tianracer.git\nRC=0\n",
            "ls": HEAD_A + "\trefs/heads/formula-mini\n",
        },
        "tianbot_core": {
            "probe": ("BRANCH=feat/serial-tap-recording\nHEAD=" + HEAD_B + "\n"
                      "SHORT=8c502d6 2026-11-07\nUP=\n"
                      "---STATUS---\n M CMakeLists.txt\n"),
            # 三斜杠 + gh-proxy：ls-remote 只吐 warning，没有 SHA
            "url": "https:///gh-proxy.com/https://github.com/tianbot/x.git\nRC=0\n",
            "ls": "warning: redirecting to https://gh-proxy.com/x/\n",
        },
        "webrtc_ros_bridge": {
            "probe": ("BRANCH=main\nHEAD=" + HEAD_A + "\n"
                      "SHORT=d917659 2026-01-15\nUP=origin/main\n"
                      "---STATUS---\n"),
            "url": "git@github.com:tianbot/y.git\nRC=0\n",
            "ls": ("Host key verification failed.\n"
                   "fatal: Could not read from remote repository.\n"),
        },
    }

    def _run_real(self, allowed):
        remote = _RealShapeRemote(self._SCRIPTS)
        return source_check.run(_Ctx(
            remote, repos=list(self._SCRIPTS), allowed=allowed)), remote

    def test_declared_patch_branch_yields_warn(self):
        res, _ = self._run_real({"tianbot_core": "feat/serial-tap-recording"})
        assert res.status is Status.WARN, res.summary
        assert "已声明" in res.summary
        assert "未推送" not in res.summary

    def test_undeclared_patch_branch_yields_fail(self):
        res, _ = self._run_real({})
        assert res.status is Status.FAIL, res.summary
        assert "未推送" in res.summary

    def test_triple_slash_is_still_diagnosed(self):
        res, _ = self._run_real({"tianbot_core": "feat/serial-tap-recording"})
        notes = " ".join(m.note for m in res.measurements)
        assert "三斜杠" in notes, notes

    def test_gitee_repo_reported_in_sync(self):
        res, _ = self._run_real({"tianbot_core": "feat/serial-tap-recording"})
        rows = _rows(res)
        assert rows.get("tianracer 相对远端") == "同步", rows

    def test_ssh_repo_unresolved(self):
        res, _ = self._run_real({"tianbot_core": "feat/serial-tap-recording"})
        assert "webrtc_ros_bridge" in res.summary


class _RealShapeRemote:
    """按 (仓库, 命令类型) 分发，模拟三个真实仓库各自的响应形状。"""

    def __init__(self, scripts):
        self.scripts = scripts
        self.commands = []

    def run(self, cmd, timeout=30, record=True):
        self.commands.append(cmd)
        repo = next((r for r in self.scripts if f"src/{r}'" in cmd), None)
        if repo is None:
            return _Result("", ok=False)
        sc = self.scripts[repo]
        if "git status --short" in cmd:
            return _Result(sc["probe"])
        if "git remote get-url" in cmd:
            return _Result(sc["url"])
        if "git ls-remote" in cmd:
            ok = "fatal" not in sc["ls"]
            return _Result(sc["ls"], ok=ok)
        return _Result("", ok=False)
