"""判定函数的边界测试。

优先级：防漏报 > 防误报。每个函数都要覆盖
「取不到数据」「下界外」「恰好在界上」「上界外」四种情况。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.verdict import (  # noqa: E402
    CheckResult, Report, Status,
    judge_all, judge_displacement, judge_distance, judge_envvar,
    judge_range, judge_repeats, judge_voltage,
)


# --------------------------------------------------------------------------
# judge_range
# --------------------------------------------------------------------------

class TestJudgeRange:
    def test_normal_value_passes(self):
        assert judge_range(30.0, 28.0, 32.0) is Status.PASS

    def test_none_is_skip_not_pass(self):
        # 关键：取不到数据不能算通过
        assert judge_range(None, 28.0, 32.0) is Status.SKIP

    def test_non_numeric_is_skip(self):
        assert judge_range("abc", 28.0, 32.0) is Status.SKIP

    def test_below_low_fails(self):
        assert judge_range(27.9, 28.0, 32.0) is Status.FAIL

    def test_above_high_fails(self):
        assert judge_range(32.1, 28.0, 32.0) is Status.FAIL

    def test_exact_lower_boundary_passes(self):
        # 边界值应算通过（>= 语义）
        assert judge_range(28.0, 28.0, 32.0) is Status.PASS

    def test_exact_upper_boundary_passes(self):
        assert judge_range(32.0, 28.0, 32.0) is Status.PASS

    def test_warn_margin_flags_near_low(self):
        assert judge_range(28.5, 28.0, 32.0, warn_margin=1.0) is Status.WARN

    def test_warn_margin_flags_near_high(self):
        assert judge_range(31.5, 28.0, 32.0, warn_margin=1.0) is Status.WARN

    def test_warn_margin_mid_still_passes(self):
        assert judge_range(30.0, 28.0, 32.0, warn_margin=1.0) is Status.PASS

    def test_zero_margin_never_warns(self):
        assert judge_range(28.0, 28.0, 32.0, warn_margin=0.0) is Status.PASS


# --------------------------------------------------------------------------
# judge_voltage
# --------------------------------------------------------------------------

class TestJudgeVoltage:
    def test_full_is_pass(self):
        assert judge_voltage(12.1, 11.7, 11.0) is Status.PASS

    def test_exactly_normal_is_pass(self):
        assert judge_voltage(11.7, 11.7, 11.0) is Status.PASS

    def test_between_is_warn(self):
        assert judge_voltage(11.3, 11.7, 11.0) is Status.WARN

    def test_exactly_low_is_warn(self):
        # 11.0 恰好等于 low，按 >= 算 WARN 不是 FAIL
        assert judge_voltage(11.0, 11.7, 11.0) is Status.WARN

    def test_below_low_is_fail(self):
        assert judge_voltage(10.97, 11.7, 11.0) is Status.FAIL

    def test_none_is_skip(self):
        assert judge_voltage(None, 11.7, 11.0) is Status.SKIP


# --------------------------------------------------------------------------
# judge_envvar
# --------------------------------------------------------------------------

class TestJudgeEnvvar:
    def test_match_passes(self):
        assert judge_envvar("/dev/ttyUSB0", "/dev/ttyUSB0") is Status.PASS

    def test_mismatch_fails(self):
        assert judge_envvar("/dev/tianbot_base", "/dev/ttyUSB0") is Status.FAIL

    def test_none_fails(self):
        # 变量不存在是明确的问题，不是「跳过」
        assert judge_envvar(None, "/dev/ttyUSB0") is Status.FAIL

    def test_known_deviation_warns(self):
        known = {"actual": "a2", "reason": "实物为 LakiBeam1"}
        assert judge_envvar("a2", "lakibeam1", known) is Status.WARN

    def test_known_deviation_does_not_match_other_values(self):
        known = {"actual": "a2", "reason": "x"}
        assert judge_envvar("b7", "lakibeam1", known) is Status.FAIL


# --------------------------------------------------------------------------
# judge_displacement / judge_distance
# --------------------------------------------------------------------------

class TestJudgeDisplacement:
    def test_tiny_movement_passes(self):
        assert judge_displacement(0.0037, 0.0005) is Status.PASS

    def test_zero_fails(self):
        assert judge_displacement(0.0, 0.0005) is Status.FAIL

    def test_negative_direction_still_counts(self):
        # 后退也是响应
        assert judge_displacement(-0.05, 0.0005) is Status.PASS

    def test_none_is_skip(self):
        assert judge_displacement(None, 0.0005) is Status.SKIP


class TestJudgeDistance:
    def test_within_tolerance_passes(self):
        assert judge_distance(0.2656, 0.30, 0.20) is Status.PASS

    def test_below_tolerance_fails(self):
        assert judge_distance(0.10, 0.30, 0.20) is Status.FAIL

    def test_exactly_at_tolerance_passes(self):
        # 0.24 vs 0.30 误差正好 20%
        assert judge_distance(0.24, 0.30, 0.20) is Status.PASS

    def test_zero_expected_is_skip_not_pass(self):
        # 除零保护，且不能静默通过
        assert judge_distance(1.0, 0.0, 0.2) is Status.SKIP

    def test_negative_direction_counts(self):
        assert judge_distance(-0.28, 0.30, 0.20) is Status.PASS

    def test_none_is_skip(self):
        assert judge_distance(None, 0.30, 0.20) is Status.SKIP


# --------------------------------------------------------------------------
# judge_repeats / judge_all
# --------------------------------------------------------------------------

class TestJudgeRepeats:
    def test_both_good_passes(self):
        assert judge_repeats([10.2, 12.0], 5.0, 17.0) is Status.PASS

    def test_one_bad_fails(self):
        # 单向合格不能放行
        assert judge_repeats([11.0, 2.0], 5.0, 17.0) is Status.FAIL

    def test_all_none_is_skip(self):
        assert judge_repeats([None, None], 5.0, 17.0) is Status.SKIP

    def test_empty_is_skip(self):
        assert judge_repeats([], 5.0, 17.0) is Status.SKIP

    def test_one_warn_makes_warn(self):
        assert judge_repeats([10.0, 6.0], 5.0, 17.0, warn_margin=2.0) is Status.WARN


class TestJudgeAll:
    def test_all_pass(self):
        assert judge_all([Status.PASS, Status.PASS]) is Status.PASS

    def test_any_fail_fails(self):
        assert judge_all([Status.PASS, Status.FAIL]) is Status.FAIL

    def test_fail_can_be_downgraded(self):
        assert judge_all([Status.PASS, Status.FAIL], fail_is_warn=True) is Status.WARN

    def test_all_skip(self):
        assert judge_all([Status.SKIP, Status.SKIP]) is Status.SKIP

    def test_empty(self):
        assert judge_all([]) is Status.SKIP


# --------------------------------------------------------------------------
# Report 汇总判定 —— 这是漏报的最后一道闸门
# --------------------------------------------------------------------------

def make(number, status, title=None, fatal=False):
    return CheckResult(number=number, title=title or f"项{number}",
                       status=status, fatal=fatal, summary="测试")


class TestReportVerdict:
    def _report(self, *results):
        report = Report(host="1.1.1.1", user="u", started_at="now")
        for r in results:
            report.add(r)
        return report

    def test_all_pass(self):
        report = self._report(*[make(i, Status.PASS) for i in range(1, 6)])
        assert report.verdict.startswith("具备交付条件")
        assert report.verdict_class == "good"

    def test_any_fail_blocks(self):
        report = self._report(make(1, Status.PASS), make(2, Status.FAIL))
        assert "不具备交付条件" in report.verdict

    def test_fatal_fail_mentions_fatal(self):
        report = self._report(make(1, Status.FAIL, fatal=True))
        assert "致命" in report.verdict

    def test_all_skip_is_not_pass(self):
        # 核心防漏报：全部 SKIP 绝不能报「具备交付条件」
        report = self._report(*[make(i, Status.SKIP) for i in range(1, 6)])
        verdict = report.verdict
        # 注意：「具备交付条件」是「不具备交付条件」的子串，
        # 所以必须从开头比对，不能用 in / not in。
        assert not verdict.startswith("具备交付条件")
        assert "未取得数据" in verdict

    def test_mostly_skip_blocks(self):
        # 10 项里只有 1 项 PASS，其余 SKIP → 通过率不足
        results = [make(1, Status.PASS)] + [make(i, Status.SKIP) for i in range(2, 11)]
        report = self._report(*results)
        assert "不具备" in report.verdict

    def test_warn_only_is_deliverable_with_note(self):
        report = self._report(make(1, Status.PASS), make(2, Status.WARN))
        assert "可交付" in report.verdict
        assert report.verdict_class == "warn"

    def test_some_skip_is_deliverable_but_flagged(self):
        results = [make(i, Status.PASS) for i in range(1, 9)] + [make(9, Status.SKIP)]
        report = self._report(*results)
        assert "可交付" in report.verdict
        assert "未验证" in report.verdict

    def test_empty_report(self):
        report = self._report()
        assert "无法判定" in report.verdict

    def test_aborted_overrides(self):
        report = self._report(make(1, Status.PASS))
        report.aborted_reason = "用户中断"
        assert "中途终止" in report.verdict

    def test_pass_ratio_calculation(self):
        report = self._report(
            make(1, Status.PASS), make(2, Status.PASS),
            make(3, Status.WARN), make(4, Status.SKIP),
        )
        # executed = PASS + WARN = 3，passed = 2
        assert report.pass_ratio == pytest.approx(2 / 3)

    def test_skip_counts_as_pass_property(self):
        assert Status.SKIP.counts_as_pass is False
        assert Status.PASS.counts_as_pass is True
        assert Status.MANUAL.counts_as_pass is True
