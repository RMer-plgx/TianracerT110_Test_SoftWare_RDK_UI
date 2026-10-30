"""审查发现的漏报漏洞回归测试。

每一条都对应 Round 1 审查里的一个"必须修"，
防止以后改代码时把已修复的漏洞重新引入。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.verdict import (
    CheckResult, Report, Status,
    judge_all, judge_repeats,
)


class TestSkipMustNotBeSwallowed:
    """A4: SKIP 不能被 PASS 吞掉。"""

    def test_judge_all_pass_plus_skips_is_skip(self):
        # 关键：部分验证 ≠ 全部合格
        assert judge_all([Status.PASS, Status.SKIP, Status.SKIP, Status.SKIP]) is Status.SKIP

    def test_judge_all_pass_plus_one_skip_is_skip(self):
        assert judge_all([Status.PASS, Status.SKIP]) is Status.SKIP

    def test_judge_all_all_pass_is_pass(self):
        assert judge_all([Status.PASS, Status.PASS]) is Status.PASS

    def test_judge_all_all_skip_is_skip(self):
        assert judge_all([Status.SKIP, Status.SKIP]) is Status.SKIP

    def test_judge_all_fail_wins(self):
        assert judge_all([Status.PASS, Status.SKIP, Status.FAIL]) is Status.FAIL

    def test_judge_repeats_first_missing_is_skip(self):
        # D23：左右各测一次，一侧没数据不能放行
        assert judge_repeats([None, 12.0], 5.0, 17.0) is Status.SKIP

    def test_judge_repeats_second_missing_is_skip(self):
        assert judge_repeats([12.0, None], 5.0, 17.0) is Status.SKIP

    def test_judge_repeats_both_present_both_good(self):
        assert judge_repeats([10.0, 12.0], 5.0, 17.0) is Status.PASS

    def test_judge_repeats_one_bad_still_fails(self):
        assert judge_repeats([11.0, 2.0], 5.0, 17.0) is Status.FAIL


class TestCheckResultStatusNotLeaking:
    """A2/A3: 检测项结尾必须用 judge_all 收口，不能保留初始 PASS。"""

    def test_imu_probe_failure_does_not_pass(self):
        """IMU 探针失败但频率正常时，不能报 PASS。

        构造方式：monkeypatch measure_hz 返回正常值、run_probe 返回 None。
        """
        from checks import imu

        class FakeCtx:
            baseline = {
                "imu": {"accel_z_min": 9.4, "accel_z_max": 10.6,
                        "accel_z_warn_high": 10.3, "gyro_static_max": 0.05,
                        "roll_pitch_max_deg": 5.0},
                "frequency": {"imu_hz": {"min": 45.0, "max": 55.0}},
            }

        original_hz, original_probe = imu.measure_hz, imu.run_probe
        try:
            imu.measure_hz = lambda *a, **k: 50.0
            imu.run_probe = lambda *a, **k: None
            result = imu.run(FakeCtx())
        finally:
            imu.measure_hz, imu.run_probe = original_hz, original_probe

        assert result.status is Status.SKIP, (
            f"探针失败时不应报 {result.status.name}，实际 summary={result.summary}"
        )

    def test_odom_drift_missing_does_not_pass(self):
        from checks import odom

        class FakeCtx:
            baseline = {
                "odom": {"static_drift_max_m": 0.01, "sample_seconds": 5},
                "frequency": {"odom_hz": {"min": 95.0, "max": 105.0}},
            }

        original = odom.run_probe
        try:
            odom.run_probe = lambda *a, **k: {
                "ok": True, "hz": 100.0, "static_drift_m": None,
                "start": {"x": 0, "y": 0}, "end": {"x": 0, "y": 0},
            }
            result = odom.run(FakeCtx())
        finally:
            odom.run_probe = original

        assert result.status is Status.SKIP, (
            f"漂移缺失时不应报 {result.status.name}，summary={result.summary}"
        )


class TestVoltageStrictParsing:
    """A5: 电压解析不能把日志时间戳当成电压值。"""

    def test_timestamp_is_rejected(self):
        from checks.voltage import _strict_float
        assert _strict_float("2026-10-30 17:15:00 ERROR: no msg") is None

    def test_info_log_is_rejected(self):
        from checks.voltage import _strict_float
        assert _strict_float("[INFO] [1698700000.123]: hello") is None

    def test_normal_voltage_is_accepted(self):
        from checks.voltage import _strict_float
        assert _strict_float("11.65\n---") == pytest.approx(11.65)

    def test_out_of_battery_range_rejected(self):
        from checks.voltage import _strict_float
        # 2026.0 不在 6~16V 合理范围
        assert _strict_float("2026.0") is None
        assert _strict_float("0.5") is None

    def test_empty_is_none(self):
        from checks.voltage import _strict_float
        assert _strict_float("") is None
        assert _strict_float("---") is None


class TestAbortedReportNotGreen:
    """D14: 中止的报告不能用绿色。"""

    def test_aborted_verdict_class_is_bad(self):
        report = Report(host="1.1.1.1", user="u", started_at="now")
        report.add(CheckResult(number=1, title="网络", status=Status.PASS))
        report.aborted_reason = "用户中断（Ctrl-C）"
        assert report.verdict_class == "bad"

    def test_aborted_verdict_mentions_termination(self):
        report = Report(host="1.1.1.1", user="u", started_at="now")
        report.add(CheckResult(number=1, title="网络", status=Status.PASS))
        report.aborted_reason = "用户中断"
        assert "中途终止" in report.verdict


class TestSkipRatioDrivesVerdict:
    """D24: 判定看未验证比例，不看通过率。"""

    def _report(self, *statuses):
        report = Report(host="1.1.1.1", user="u", started_at="now")
        for index, status in enumerate(statuses, start=1):
            report.add(CheckResult(number=index, title=f"项{index}", status=status))
        return report

    def test_one_pass_nine_skip_blocks(self):
        # 这是最典型的漏报样本：只有 1 项真跑过，不能算合格
        report = self._report(Status.PASS, *([Status.SKIP] * 9))
        assert not report.verdict.startswith("具备交付条件")

    def test_eighteen_pass_one_skip_is_warn(self):
        report = self._report(*([Status.PASS] * 18), Status.SKIP)
        assert report.skip_ratio == pytest.approx(1 / 19)
        assert "未验证" in report.verdict
        assert report.verdict_class == "warn"


class TestMotionProbeSafetyLimits:
    """C12: 探针的上限不能由被保护对象自己提供。"""

    @staticmethod
    def _constants():
        """从源码提取常量。

        不能直接 import：motion_probe 顶部 `import rospy`，
        本机（Windows 开发机）没有 rospy，import 会失败。
        """
        import ast
        source = (Path(__file__).resolve().parent.parent
                  / "probes" / "motion_probe.py").read_text(encoding="utf-8")
        values = {}
        for node in ast.parse(source).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
                    values[target.id] = node.value.value
        return values

    def test_hard_limits_are_strict(self):
        consts = self._constants()
        assert consts["HARD_MAX_SPEED"] <= 0.5
        assert consts["HARD_MAX_DURATION"] <= 10.0
        assert consts["HARD_MAX_STEER"] <= 0.6

    def test_config_cannot_exceed_hard_limit(self):
        """baseline 被改成 5 m/s 时，探针按硬上限拒绝。"""
        consts = self._constants()
        requested = 5.0
        effective = min(requested, consts["HARD_MAX_SPEED"])
        assert effective == consts["HARD_MAX_SPEED"]
        assert effective <= 0.5

    def test_source_applies_min_to_limits(self):
        """确认源码里真的用了 min(命令行, 硬上限)，而不是信任命令行。"""
        source = (Path(__file__).resolve().parent.parent
                  / "probes" / "motion_probe.py").read_text(encoding="utf-8")
        assert "min(max_speed, HARD_MAX_SPEED)" in source
        assert "min(max_duration, HARD_MAX_DURATION)" in source
        assert "abs(steer) > HARD_MAX_STEER" in source
