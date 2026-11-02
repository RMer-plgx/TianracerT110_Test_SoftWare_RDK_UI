"""提示级（NOTICE）的语义测试。

背景（2026-11-02）：出货报告如果满屏黄条，出货人员每一条都得自己判断
「这车到底能不能发」，反而拖慢出货。于是把「对出货无影响的差异」
单独分成 NOTICE：显示绿色、计入通过，但事实照样写进报告。

这组测试守住三条底线：
  1. NOTICE 计入通过 —— 报告能显示「已执行项通过 19 / 19」
  2. NOTICE 不掩盖 —— 原始 summary 必须还在，且有独立的提示项清单
  3. NOTICE 不放松真问题 —— WARN / FAIL / SKIP 的判定一律不受影响
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.verdict import (  # noqa: E402
    MAX_SKIP_RATIO, CheckResult, Report, Status,
)


def _report(*statuses, aborted: str = "") -> Report:
    report = Report(host="10.0.0.9", user="sunrise",
                    started_at="2026-11-02 10:00:00")
    report.aborted_reason = aborted
    for index, status in enumerate(statuses, start=1):
        report.add(CheckResult(number=index, title=f"项{index}",
                               status=status, summary=f"项{index} 的事实"))
    return report


class TestNoticeCountsAsPass:
    def test_notice_counts_as_pass(self):
        assert Status.NOTICE.counts_as_pass is True

    def test_skip_still_does_not_count(self):
        # 防漏报的核心不能因为引入 NOTICE 被带松
        assert Status.SKIP.counts_as_pass is False

    def test_notice_marker_is_green_check(self):
        # 出货人员看的是符号，必须和 PASS 一致
        assert Status.NOTICE.marker == Status.PASS.marker

    def test_notice_label_reads_pass(self):
        assert Status.NOTICE.label == "PASS"


class TestNoticeInReport:
    def test_all_notice_reports_full_pass(self):
        report = _report(*[Status.NOTICE] * 19)
        assert len(report.passed) == 19
        assert len(report.executed) == 19
        assert report.pass_ratio == 1.0

    def test_verdict_is_green_when_only_notice(self):
        report = _report(*[Status.NOTICE] * 19)
        assert report.verdict == "具备交付条件"
        assert report.verdict_class == "good"

    def test_notice_listed_separately_for_audit(self):
        report = _report(Status.PASS, Status.NOTICE, Status.PASS)
        assert len(report.noticed) == 1
        assert report.noticed[0].summary == "项2 的事实"

    def test_notice_not_in_warned(self):
        # warned 触发「需确认」措辞，NOTICE 不能混进去
        report = _report(Status.NOTICE, Status.NOTICE)
        assert report.warned == []


class TestNoticeDoesNotHideRealProblems:
    def test_notice_plus_fail_still_fails(self):
        report = _report(Status.NOTICE, Status.FAIL)
        assert report.verdict_class == "bad"
        assert "不具备交付条件" in report.verdict

    def test_notice_plus_warn_still_needs_confirmation(self):
        report = _report(Status.NOTICE, Status.WARN)
        assert report.verdict_class == "warn"
        assert "告警" in report.verdict

    def test_notice_plus_too_many_skips_still_fails(self):
        # 1 个 PASS/NOTICE + 大量 SKIP：不能因为有一个绿点就放行
        report = _report(Status.NOTICE, *[Status.SKIP] * 4)
        assert report.skip_ratio > MAX_SKIP_RATIO
        assert report.verdict_class == "bad"

    def test_aborted_report_never_green(self):
        report = _report(*[Status.NOTICE] * 19, aborted="致命项失败：激光雷达")
        assert report.verdict_class == "bad"


class TestNoticeRenders:
    def test_html_shows_notice_section_and_pass_label(self, tmp_path):
        from core.reporting import render

        report = _report(Status.PASS, Status.NOTICE)
        report.host = "10.0.0.9"
        _, html_path = render(report, tmp_path)
        text = html_path.read_text(encoding="utf-8")

        # 打印出来是 PASS，不是 NOTICE
        assert "NOTICE" not in text
        # 但事实保留在提示项区
        assert "提示项（不影响出货）" in text
        assert "项2 的事实" in text

    def test_markdown_shows_notice_section(self, tmp_path):
        from core.reporting import render

        report = _report(Status.NOTICE)
        md_path, _ = render(report, tmp_path)
        text = md_path.read_text(encoding="utf-8")
        assert "提示项（不影响出货）" in text
        assert "项1 的事实" in text
