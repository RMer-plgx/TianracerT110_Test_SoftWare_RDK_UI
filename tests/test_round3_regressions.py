"""Round 3 审查发现的缺陷回归测试。

核心是两条：
1. 陈旧的 /tmp/tt_voltage.txt 不能让低电压车被判合格
2. tap 在 rosmaster 就绪前执行会立即失败（等于白做）

以及一条一致性断言：环境变量白名单必须与 ROS_ENV_PREFIX 同步。
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._fixtures import attach_profile, get_profile  # noqa: E402
from core.verdict import Status


# ---------------------------------------------------------------------------
# Round 3 - 1: 陈旧 tap 文件会顶替本次电压
# ---------------------------------------------------------------------------

class TestStaleTapFile:
    """这是本轮最危险的一条：低电压车会拿到旧值并放行运动测试。"""

    @staticmethod
    def _ctx(remote, owns_bringup=True):
        class FakeCtx:
            profile = get_profile()
            baseline = {
                "voltage": {"normal_v": 11.7, "low_v": 11.0, "full_v": 12.4,
                            "stop_test_below_v": 11.0, "sample_timeout_sec": 90},
            }
            owns_bringup = None

            def say(self, text):
                pass

        ctx = FakeCtx()
        ctx.remote = remote
        ctx.owns_bringup = owns_bringup
        return ctx

    def test_current_subscription_wins_over_stale_file(self):
        """现订阅读到 10.50 V，tap 文件残留 11.65 → 必须按 10.50 判 FAIL。"""
        from checks import voltage

        class FakeRemote:
            def run(self, cmd, timeout=60, record=True):
                class R:
                    exit_code = 0
                    stderr = ""
                    stdout = ""

                r = R()
                if "voltage/data" in cmd and "rostopic" in cmd:
                    r.stdout = "10.500000000000000\n---\n"   # 真实电压：低
                elif "cat /tmp/tt_voltage.txt" in cmd:
                    r.stdout = "11.65\n---\n"                # 残留的旧值
                elif "rosnode list" in cmd:
                    r.stdout = "/tianracer/tianracer\n"
                return r

        result = voltage.run(self._ctx(FakeRemote()))
        assert result.status is Status.FAIL, (
            f"低电压车不应因陈旧 tap 文件被判合格，实际 {result.status.name} / {result.summary}"
        )
        assert "10.50" in " ".join(m.value for m in result.measurements)

    def test_attach_mode_never_reads_tap(self):
        """attach 模式下不能读 tap（tap 只由 auto 模式产生）。"""
        from checks import voltage

        read_tap = {"hit": False}

        class FakeRemote:
            def run(self, cmd, timeout=60, record=True):
                class R:
                    exit_code = 0
                    stderr = ""
                    stdout = ""

                r = R()
                if "cat /tmp/tt_voltage.txt" in cmd:
                    read_tap["hit"] = True
                    r.stdout = "11.65\n"
                elif "voltage/data" in cmd and "rostopic" in cmd:
                    r.stdout = "(no output)"      # 现订阅失败
                elif "rosnode list" in cmd:
                    r.stdout = "/tianracer/tianracer\n"
                return r

        result = voltage.run(self._ctx(FakeRemote(), owns_bringup=False))
        assert not read_tap["hit"], "attach 模式不应读 tap 文件"
        assert result.status is Status.FAIL


# ---------------------------------------------------------------------------
# Round 3 - 2: tap 必须在 rosmaster 就绪后才启动
# ---------------------------------------------------------------------------

class TestTapTiming:
    def test_wait_for_master_exists(self):
        from core import remote as m
        assert hasattr(m, "wait_for_master")

    def test_runner_waits_before_tap(self):
        import inspect
        from core import runner
        source = inspect.getsource(runner)
        wait_at = source.index("wait_for_master")
        tap_at = source.index("start_voltage_tap")
        assert wait_at < tap_at, "wait_for_master 必须在 start_voltage_tap 之前"

    def test_tap_clears_old_file_first(self):
        import inspect
        from core import remote as m
        source = inspect.getsource(m.start_voltage_tap)
        assert "rm -f" in source, "tap 启动前必须先删旧文件"

    def test_runner_clears_tap_at_start_and_end(self):
        """收尾清理必须位于 close() 之前。

        上一版这个测试只断言 `source.count("VOLTAGE_TAP_PATH") >= 2`，
        而收尾那段其实是死代码（连接已关，run() 直接 raise），
        测试依旧是绿的（Round 4 审查发现）。
        现在改成结构断言：检查 _run_inner 的 finally 里，
        rm 的语句位置必须在 close 之前。
        """
        import inspect
        from core.runner import Runner

        source = inspect.getsource(Runner._run_inner)
        close_at = source.rfind("self._remote.close()")
        assert close_at != -1, "_run_inner 里找不到 close()"

        # close 之前的所有 rm 里，必须有一处在收尾的 finally 段（而不是只靠开头那次）
        before = source[:close_at]
        rm_positions = [i for i in range(len(before))
                        if before.startswith("VOLTAGE_TAP_PATH", i)]
        assert len(rm_positions) >= 2, (
            f"close() 之前只有 {len(rm_positions)} 处 tap 清理，"
            "应该有开头 + 收尾两处；只有一处说明收尾那段不在 finally 里"
        )
        # 离 close 最近的那处，必须在 finally 块内（即 close 之前不远处）
        last_rm = rm_positions[-1]
        assert close_at - last_rm < 600, (
            "最近的 tap 清理离 close() 太远，可能在函数开头而不是收尾 finally"
        )

    def test_no_cleanup_in_finalize(self):
        """_finalize 在连接关闭后才调用，里面不能有 rm（会是死代码）。"""
        import inspect
        from core.runner import Runner
        source = inspect.getsource(Runner._finalize)
        assert "rm -f" not in source

    def test_tap_timeout_is_long_enough(self):
        import inspect
        from core import remote as m
        source = inspect.getsource(m.start_voltage_tap)
        match = re.search(r"timeout (\d+) rostopic", source)
        assert match, "找不到 tap 的 timeout"
        # runtime 阶段最坏约 129 秒，120 不够
        assert int(match.group(1)) >= 180


# ---------------------------------------------------------------------------
# Round 3 - 建议: 白名单必须与 ROS_ENV_PREFIX 同步
# ---------------------------------------------------------------------------

class TestEnvvarWhitelistConsistency:
    def test_whitelist_matches_prefix_exports(self):
        from core import profile as product_profile
        from checks.envvars import OVERRIDDEN_BY_PREFIX
        ROS_ENV_PREFIX = product_profile.load().ros_env_prefix()

        exported = set(re.findall(r"export (TIAN\w+)=", ROS_ENV_PREFIX))
        assert exported, "ROS_ENV_PREFIX 里没找到 export TIAN* 变量"
        assert OVERRIDDEN_BY_PREFIX == exported, (
            f"白名单与实际导出不一致。\n"
            f"  只在白名单：{OVERRIDDEN_BY_PREFIX - exported}\n"
            f"  只在前缀：  {exported - OVERRIDDEN_BY_PREFIX}"
        )


# ---------------------------------------------------------------------------
# Round 3 - 4: 错别字
# ---------------------------------------------------------------------------

class TestNoTypo:
    def test_voltage_typo_fixed(self):
        source = (Path(__file__).resolve().parent.parent
                  / "checks" / "voltage.py").read_text(encoding="utf-8")
        assert "电压话量" not in source, "错别字未修：应为「电压话题」"
