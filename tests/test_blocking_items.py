"""阻断交付的项：报告要回答「哪一环节的什么值出了什么问题」。

2026-11-17 用户要求：

> 检测时哪一项出现失败项，要在检测报告中明确指出，不具备交付条件：
> 哪一环节的什么项值出现了什么问题

R26-1（审查）：只列 FAIL 不够 —— `verdict` 有五条阻断路径，
「致命项停在 PENDING」与「未验证比例超限」都不会产生 FAIL，
若不列，报告会出现「红色判定 + 下方一片空白」，读者会误以为没有具体问题。

覆盖四个要素：环节（phase） / 检测项 / 项值（value+expected） / 问题（summary）。
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.verdict import (  # noqa: E402
    MAX_SKIP_RATIO, CheckResult, Report, Status, phase_label,
)


def _report(*results):
    report = Report(host="192.168.1.82", user="sunrise",
                    started_at="2026-11-17 10:00:00")
    for item in results:
        report.add(item)
    return report


def _passthrough(count, start=1):
    return [CheckResult(number=start + i, title=f"项{start + i}",
                        status=Status.PASS, phase="static", summary="ok")
            for i in range(count)]


def _voltage_fail():
    item = CheckResult(number=16, title="电源与电池电压",
                       status=Status.FAIL, phase="runtime", fatal=False,
                       summary="10.40 V 低于交付线 11.0 V，必须充电。后续运动测试将被跳过")
    item.add("采样耗时", "3.2", "s")
    item.add("电压", "10.40", "V", expected="≥ 11.0 V（满电 12.6 V）")
    return item


class TestBlockingItemDetail:
    """每个阻断项必须给出环节 / 项值 / 问题。"""

    def test_reports_phase_number_value_and_problem(self):
        items = _report(_voltage_fail()).blocking_items
        assert len(items) == 1
        entry = items[0]
        assert entry["phase"] == "runtime"
        assert entry["phase_label"] == phase_label("runtime")
        assert "运行时" in entry["phase_label"]
        assert entry["number"] == 16
        assert entry["title"] == "电源与电池电压"
        assert entry["kind"] == "fail"
        assert entry["reason"] == "失败"
        assert "10.40 V" in entry["values_text"][0]
        assert "≥ 11.0 V" in entry["values_text"][0]
        assert "必须充电" in entry["summary"]

    def test_fatal_fail_reason_is_distinct(self):
        item = CheckResult(number=6, title="主控平台", status=Status.FAIL,
                           fatal=True, phase="static", summary="不是 RDK X5")
        entry = _report(item).blocking_items[0]
        assert entry["reason"] == "致命项失败"
        assert entry["fatal"] is True

    def test_only_measurements_with_expected_are_listed(self):
        """「采样耗时 3.2 s」不是失败原因，不该混进「项值」。"""
        values = _report(_voltage_fail()).blocking_items[0]["values_text"]
        assert not any("采样耗时" in v for v in values), values

    def test_falls_back_to_all_measurements_when_no_expected(self):
        """像「串口缺失」这种 FAIL 没有带期望的测量，也不能显示空。"""
        item = CheckResult(number=5, title="设备节点", status=Status.FAIL,
                           phase="static", summary="底盘串口 /dev/ttyUSB0 缺失")
        item.add("串口", "缺失")
        values = _report(item).blocking_items[0]["values_text"]
        assert values == ["串口 缺失"], values

    def test_no_measurements_still_keeps_problem(self):
        item = CheckResult(number=6, title="主控平台", status=Status.FAIL,
                           phase="static", summary="不是 RDK X5 平台")
        entry = _report(item).blocking_items[0]
        assert entry["values_text"] == []
        assert entry["summary"] == "不是 RDK X5 平台"

    def test_sorted_by_number(self):
        report = _report(
            CheckResult(number=16, title="电压", status=Status.FAIL),
            CheckResult(number=5, title="设备节点", status=Status.FAIL),
        )
        assert [i["number"] for i in report.blocking_items] == [5, 16]

    def test_empty_when_everything_passes(self):
        report = _report(*_passthrough(3))
        assert report.blocking_items == []
        assert report.blocking_note == ""


class TestPathTwoFatalPending:
    """R26-1 路径 2：致命项停在 PENDING（没做落地互校）。

    这是最常见的真实报告 —— 车没场地做落地互校时 IMU/里程计必然 PENDING，
    verdict 是「不具备交付条件（致命项未完成落地互校）」。
    """

    def _scenario_c(self):
        """18 PASS + 2 致命 PENDING → 未验证 2/20 = 10%，不超限。"""
        items = _passthrough(18)
        for number, title in ((19, "IMU"), (20, "里程计")):
            items.append(CheckResult(
                number=number, title=title, status=Status.PENDING,
                phase="runtime", fatal=True, needs_ground=True,
                summary="100 Hz；航向精度待落地互校"))
        return _report(*items)

    def test_verdict_blocks(self):
        report = self._scenario_c()
        assert report.verdict.startswith("不具备交付条件")
        assert "落地互校" in report.verdict

    def test_pending_items_are_listed(self):
        report = self._scenario_c()
        items = report.blocking_items
        assert [i["number"] for i in items] == [19, 20]
        assert all(i["kind"] == "pending" for i in items)
        assert all(i["reason"] == "致命项未完成落地互校" for i in items)
        assert all(i["phase_label"] == phase_label("runtime") for i in items)

    def test_skip_ratio_is_not_the_trigger(self):
        """确认这不是被路径 3（比例超限）捎带列出的。"""
        report = self._scenario_c()
        assert report.skip_ratio <= MAX_SKIP_RATIO

    def test_rendered_reports_have_the_block(self, tmp_path):
        report = self._scenario_c()
        md, html = _render(report, tmp_path)
        for text, name in ((md, "report.md"), (html, "report.html")):
            assert "阻断交付的原因" in text, name
            assert "致命项未完成落地互校" in text, name
            assert "运行时不" not in text  # 防手滑，实际用 phase_label
            assert "运行时检查" in text, name


class TestPathThreeUnverifiedRatio:
    """R26-1 路径 3：未验证比例超限（几项 SKIP 就能超过 20%）。"""

    def _scenario_b(self):
        """15 PASS + 4 SKIP + 1 PENDING → 5/20 = 25% > 20%。"""
        items = _passthrough(15)
        for offset, number in enumerate(range(16, 20)):
            items.append(CheckResult(
                number=number, title=f"跳过项{number}", status=Status.SKIP,
                phase="static", summary="用户未允许车辆微动"))
        items.append(CheckResult(
            number=20, title="IMU", status=Status.PENDING,
            phase="runtime", summary="航向精度待落地互校"))
        return _report(*items)

    def test_verdict_blocks_on_ratio(self):
        report = self._scenario_b()
        assert report.verdict.startswith("不具备交付条件")
        assert "未验证" in report.verdict
        assert report.skip_ratio > MAX_SKIP_RATIO

    def test_unverified_items_are_listed_with_phase(self):
        report = self._scenario_b()
        items = report.blocking_items
        assert [i["number"] for i in items] == [16, 17, 18, 19, 20]
        assert {i["kind"] for i in items} == {"skip", "pending"}
        assert all(i["reason"] for i in items)
        phases = {i["number"]: i["phase_label"] for i in items}
        assert phases[16] == phase_label("static")
        assert phases[20] == phase_label("runtime")

    def test_rendered_reports_have_the_block(self, tmp_path):
        report = self._scenario_b()
        md, html = _render(report, tmp_path)
        for text, name in ((md, "report.md"), (html, "report.html")):
            assert "阻断交付的原因" in text, name
            assert "未验证比例超限" in text, name
            assert "静态检查" in text, name


class TestBlockingPathsDoNotOverlap:
    """同一项不重复列，且未超限时 SKIP/PENDING 不该被扫进来。"""

    def test_skip_and_pending_not_listed_below_ratio(self):
        """1 SKIP / 10 项 = 10%，未超限 → 不算阻断项。"""
        items = _passthrough(9)
        items.append(CheckResult(number=10, title="雷达", status=Status.SKIP,
                                 phase="static", summary="未连接"))
        report = _report(*items)
        assert report.skip_ratio <= MAX_SKIP_RATIO
        assert report.blocking_items == []

    def test_fatal_pending_listed_once(self):
        """同时满足「致命 PENDING」和「比例超限」时只出现一次。"""
        items = _passthrough(2)
        items.append(CheckResult(number=3, title="IMU", status=Status.PENDING,
                                 fatal=True, phase="runtime", summary="待互校"))
        items.extend(CheckResult(number=n, title=f"跳过{n}", status=Status.SKIP,
                                 phase="static", summary="未执行")
                     for n in (4, 5, 6))
        report = _report(*items)
        assert report.skip_ratio > MAX_SKIP_RATIO
        numbers = [i["number"] for i in report.blocking_items]
        assert numbers.count(3) == 1
        # 致命 PENDING 的理由优先于「比例超限」
        assert report.blocking_items[0]["reason"] == "致命项未完成落地互校"

    def test_aborted_does_not_sweep_all_unverified(self):
        """中止时不该把所有未验证项都当独立问题列出（中止本身是原因）。"""
        items = _passthrough(2)
        items.extend(CheckResult(number=n, title=f"未执行{n}", status=Status.SKIP,
                                 phase="runtime", summary="用户手动停止")
                     for n in range(3, 9))
        report = _report(*items)
        report.aborted_reason = "bringup 启动失败"
        assert report.skip_ratio > MAX_SKIP_RATIO
        assert report.blocking_items == []
        assert "中途终止" in report.blocking_note
        assert "bringup 启动失败" in report.blocking_note


class TestBlockingNote:
    """没有具体项时也要有一句说明，不能只留空白。"""

    def test_no_results(self):
        report = Report(host="h", user="u", started_at="2026-11-17 10:00:00")
        assert report.blocking_note
        assert "未产生任何检测结果" in report.blocking_note

    def test_aborted(self):
        report = _report(*_passthrough(2))
        report.aborted_reason = "程序异常：TimeoutError"
        assert "中途终止" in report.blocking_note

    def test_all_skipped(self):
        items = [CheckResult(number=n, title=f"项{n}", status=Status.SKIP,
                             phase="static", summary="未执行")
                 for n in range(1, 4)]
        report = _report(*items)
        assert "均未取得数据" in report.blocking_note

    def test_empty_when_deliverable(self):
        assert _report(*_passthrough(3)).blocking_note == ""


class TestPhaseLabel:
    def test_known_phases(self):
        assert "静态" in phase_label("static")
        assert "运行时" in phase_label("runtime")
        assert "运动" in phase_label("motion")

    def test_unknown_phase_is_not_swallowed(self):
        """新 phase 没登记时原样显示，不能悄悄变成「静态检查」。"""
        assert phase_label("thermal") == "thermal"

    def test_empty_phase(self):
        assert phase_label("") == "未标注阶段"


def _render(report, tmp_path):
    from core.reporting import render
    md, html = render(report, tmp_path)
    return (md.read_text(encoding="utf-8"),
            html.read_text(encoding="utf-8"))


class TestReportRendersBlockingSection:
    @staticmethod
    def _render(report, tmp_path):
        return _render(report, tmp_path)

    def test_html_and_md_show_phase_value_and_problem(self, tmp_path):
        report = _report(_voltage_fail())
        md, html = self._render(report, tmp_path)
        for text, name in ((md, "report.md"), (html, "report.html")):
            assert "阻断交付的原因" in text, name
            assert "运行时检查" in text, name
            assert "10.40 V" in text, name
            assert "≥ 11.0 V" in text, name
            assert "必须充电" in text, name

    def test_verdict_still_says_not_deliverable(self, tmp_path):
        report = _report(_voltage_fail())
        md, html = self._render(report, tmp_path)
        assert report.verdict.startswith("不具备交付条件")
        assert "不具备交付条件" in md
        assert "不具备交付条件" in html

    def test_no_block_when_all_pass(self, tmp_path):
        report = _report(*_passthrough(3))
        md, html = self._render(report, tmp_path)
        assert "阻断交付的原因" not in md
        assert "阻断交付的原因" not in html

    def test_newline_in_summary_keeps_md_table_four_columns(self, tmp_path):
        """R26-3：summary 含换行时不能把 md 表格行拆成两行。"""
        item = CheckResult(number=16, title="电压", status=Status.FAIL,
                           phase="runtime",
                           summary="第一行说明\n第二行说明")
        item.add("电压", "10.40", "V", expected="≥ 11.0 V")
        md, _html = self._render(_report(item), tmp_path)

        rows = [line for line in md.splitlines()
                if line.startswith("| 运行时检查")]
        assert len(rows) == 1, rows
        assert rows[0].count("|") == 5, rows[0]   # 4 列 = 5 个竖线
        assert "第一行说明 第二行说明" in rows[0]

    def test_unverified_tables_show_phase_column(self, tmp_path):
        items = _passthrough(2)
        items.append(CheckResult(number=3, title="IMU", status=Status.PENDING,
                                 phase="runtime", summary="待互校"))
        items.append(CheckResult(number=4, title="雷达", status=Status.SKIP,
                                 phase="static", summary="未连接"))
        md, html = self._render(_report(*items), tmp_path)
        for text, name in ((md, "report.md"), (html, "report.html")):
            assert "环节" in text, name
            assert "运行时检查" in text, name
            assert "静态检查" in text, name


class TestRunnerFillsPhase:
    """phase 必须来自 profile 的 checks.items。

    原来 `_execute` 解包成 `_phase` 之后把它丢掉了，于是报告里
    所有项的「环节」都是默认 static —— 也就指不出是哪一环节。
    """

    def _run_one(self, monkeypatch):
        from core import runner
        from core.verdict import CheckResult as CR

        module = types.ModuleType("checks.fake_phase_module")

        def run(ctx):
            item = CR(number=16, title="电源与电池电压", status=Status.FAIL,
                      summary="电压偏低")
            item.add("电压", "10.40", "V", expected="≥ 11.0 V")
            return item

        module.run = run
        monkeypatch.setitem(sys.modules, "checks.fake_phase_module", module)

        class Ctx:
            motion_armed = True
            cancel_requested = False
            on_check_start = None
            on_check_done = None

            def say(self, *args, **kwargs):
                pass

        runner_obj = runner.Runner.__new__(runner.Runner)
        runner_obj.report = Report(host="h", user="u",
                                   started_at="2026-11-17 10:00:00")
        runner_obj._lock = None
        runner_obj._execute(Ctx(), (16, "电源与电池电压", "fake_phase_module",
                                    "run", False, "runtime"))
        return runner_obj.report

    def test_phase_from_profile_reaches_the_report(self, monkeypatch):
        report = self._run_one(monkeypatch)
        assert report.results[0].phase == "runtime"
        assert report.blocking_items[0]["phase_label"] == phase_label("runtime")

    def test_skipped_item_also_carries_phase(self):
        """用户中止时也要能说清是哪一环节被跳过。"""
        from core import runner

        class Ctx:
            motion_armed = False
            cancel_requested = True
            on_check_start = None
            on_check_done = None

            def say(self, *args, **kwargs):
                pass

        runner_obj = runner.Runner.__new__(runner.Runner)
        runner_obj.report = Report(host="h", user="u",
                                   started_at="2026-11-17 10:00:00")
        runner_obj._lock = None
        runner_obj._execute(Ctx(), (20, "运动控制", "motion", "run",
                                    False, "motion"))
        result = runner_obj.report.results[0]
        assert result.status is Status.SKIP
        assert result.phase == "motion"
