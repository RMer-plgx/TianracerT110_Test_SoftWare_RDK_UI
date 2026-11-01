"""Round 2 审查发现的缺陷回归测试。

对应 REVIEW_FEEDBACK.md 的 Round 2 段落，每条都要能独立复现"修复前会错"。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.verdict import CheckResult, Report, Status


# ---------------------------------------------------------------------------
# Round 2 - 1: 只做微脉冲时第 18 项不能判 PASS
# ---------------------------------------------------------------------------

class TestMicroPulseOnlyIsNotPass:
    """只验证电机响应 ≠ 验证行驶精度。"""

    @staticmethod
    def _ctx(voltage_known=True, voltage_v=11.8):
        class FakeCtx:
            baseline = {
                "motion": {
                    "micro_pulse": {"speed": 0.05, "duration": 0.3,
                                    "min_displacement_m": 0.0005},
                    "safety": {"max_speed_mps": 0.5, "max_duration_sec": 10.0},
                    "bench": {}, "ground": {},
                },
            }
            motion_level = "none"
            motion_armed = True
            force_motion = False
            voltage_stop = False

            def say(self, text):
                pass

        ctx = FakeCtx()
        ctx.voltage_known = voltage_known
        ctx.voltage_v = voltage_v
        return ctx

    def test_pulse_pass_yields_warn_not_pass(self, monkeypatch):
        from checks import motion

        monkeypatch.setattr(motion, "run_probe", lambda *a, **k: {
            "ok": True, "dist_m": 0.0037, "dyaw_deg": 0.0,
        })
        result = motion.run(self._ctx())
        assert result.status is Status.WARN, (
            f"只做微脉冲时不应判 {result.status.name}，否则整车会显示『具备交付条件』"
        )
        assert "未测" in result.summary

    def test_pulse_pass_does_not_make_report_deliverable(self, monkeypatch):
        """端到端：19 项全 PASS，其中第 18 项来自微脉冲 → 不能『具备交付条件』。"""
        from checks import motion

        monkeypatch.setattr(motion, "run_probe", lambda *a, **k: {
            "ok": True, "dist_m": 0.0037, "dyaw_deg": 0.0,
        })
        check18 = motion.run(self._ctx())

        report = Report(host="1.1.1.1", user="u", started_at="now")
        for i in range(1, 18):
            report.add(CheckResult(number=i, title=f"项{i}", status=Status.PASS))
        report.add(check18)

        assert not report.verdict.startswith("具备交付条件"), (
            f"实际判定：{report.verdict}"
        )

    def test_pulse_fail_still_fails(self, monkeypatch):
        from checks import motion
        monkeypatch.setattr(motion, "run_probe", lambda *a, **k: {
            "ok": True, "dist_m": 0.0, "dyaw_deg": 0.0,
        })
        result = motion.run(self._ctx())
        assert result.status is Status.FAIL

    def test_pulse_dist_none_no_crash(self, monkeypatch):
        """dist 为 None 时格式化不能抛 TypeError。"""
        from checks import motion
        monkeypatch.setattr(motion, "run_probe", lambda *a, **k: {
            "ok": True, "dist_m": None, "dyaw_deg": 0.0,
        })
        result = motion.run(self._ctx())
        assert result.status is not Status.PASS


# ---------------------------------------------------------------------------
# Round 2 - 3: detail 不能声称覆盖了没覆盖的变量
# ---------------------------------------------------------------------------

class TestEnvvarOverrideHonesty:
    def test_only_prefix_vars_marked_overridden(self):
        from checks import envvars

        class FakeCtx:
            baseline = {
                "envvars": {
                    "TIANRACER_BASE_PORT": "/dev/ttyUSB0",   # 在白名单
                    "TIANRACER_BASE": "compact",             # 不在白名单
                },
                "envvars_known_deviations": {},
            }

            def __init__(self):
                self.remote = None

        actual = {
            "TIANRACER_BASE_PORT": "/dev/tianbot_base",
            "TIANRACER_BASE": "standard",
        }

        import core.remote as remote_mod
        original = remote_mod.read_env_vars
        try:
            remote_mod.read_env_vars = lambda _r: actual
            import importlib
            importlib.reload(envvars)
            result = envvars.run(FakeCtx())
        finally:
            remote_mod.read_env_vars = original
            importlib.reload(envvars)

        detail = result.detail or ""
        # BASE_PORT 真的被覆盖了，可以列
        assert "TIANRACER_BASE_PORT" in detail
        # BASE 没被覆盖，必须列在「未修正」而不是「已覆盖」
        assert "不在自动修正白名单" in detail or "未修正" in detail


# ---------------------------------------------------------------------------
# Round 2 - 4: TF 单个真断链不能当根节点吞掉
# ---------------------------------------------------------------------------

class TestTfDanglingFrame:
    def test_single_dangling_frame_is_warned(self, monkeypatch):
        from checks import tf

        class FakeCtx:
            baseline = {"tf": {"required_frames": [], "required_transforms": []}}

            def __init__(self):
                self.remote = None

        frames = {
            "tianracer/base_link": "tianracer/odom",
            "tianracer/odom": "tianracer/map",
            "tianracer/map": None,             # 合法根
            "tianracer/dangling": None,        # 真断链
        }
        monkeypatch.setattr(tf, "run_probe", lambda *a, **k: {
            "ok": True, "frame_count": len(frames), "frames": frames,
        })
        result = tf.run(FakeCtx())
        assert result.status is Status.WARN, (
            f"单个断链不应被吞，实际 {result.status.name} / {result.summary}"
        )
        assert "dangling" in result.summary

    def test_only_legal_root_is_pass(self, monkeypatch):
        from checks import tf

        class FakeCtx:
            baseline = {"tf": {"required_frames": [], "required_transforms": []}}

            def __init__(self):
                self.remote = None

        frames = {
            "tianracer/base_link": "tianracer/odom",
            "tianracer/odom": "tianracer/map",
            "tianracer/map": None,
        }
        monkeypatch.setattr(tf, "run_probe", lambda *a, **k: {
            "ok": True, "frame_count": len(frames), "frames": frames,
        })
        result = tf.run(FakeCtx())
        assert result.status is Status.PASS


# ---------------------------------------------------------------------------
# Round 2 - 5: 电压需在 roslaunch 后立刻抢订阅
# ---------------------------------------------------------------------------

class TestVoltageTap:
    def test_tap_function_exists_and_redirects(self):
        from core import remote as remote_mod
        assert hasattr(remote_mod, "start_voltage_tap")
        import inspect
        source = inspect.getsource(remote_mod.start_voltage_tap)
        # 必须把输出落到文件，并抢 /voltage/data
        assert "/tianracer/voltage/data" in source
        assert "VOLTAGE_TAP_PATH" in source or "/tmp/tt_voltage.txt" in source
        # 后台启动必须断掉所有 fd，避免 SSH 通道挂死
        assert "< /dev/null" in source

    def test_runner_calls_tap_after_launch(self):
        import inspect
        from core import runner
        source = inspect.getsource(runner)
        launch_at = source.index("start_launch")
        tap_at = source.index("start_voltage_tap")
        assert launch_at < tap_at, "start_voltage_tap 必须在 start_launch 之后立刻调用"
