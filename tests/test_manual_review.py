"""人工复核机制测试。

2026-11-03 用户要求：
- 程序只报事实，黄条不自己变绿
- 由人在报告 HTML 里逐条「确认接受」，勾了才转绿
- 「下载 PDF」按钮在 HTML 报告内部

这组测试守住三条：
  1. checks 模块不再自己把 WARN 降级（事实不被程序改写）
  2. HTML 只对 WARN/SKIP 给确认框，FAIL 绝不给
  3. HTML 自带下载 PDF 与打印按钮，且勾选状态可持久化
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.verdict import CheckResult, Report, Status  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _render(results, tmp_path):
    from core.reporting import render

    report = Report(host="10.0.0.9", user="sunrise",
                    started_at="2026-11-03 10:00:00")
    report.finished_at = "2026-11-03 10:05:00"
    for item in results:
        report.add(item)
    md, html = render(report, tmp_path)
    return (html.read_text(encoding="utf-8"),
            md.read_text(encoding="utf-8"))


def _item(number, title, status, summary="事实描述"):
    return CheckResult(number=number, title=title, status=status, summary=summary)


class TestNoSelfDowngrade:
    def test_status_has_no_notice(self):
        # NOTICE 已被移除：程序不能再自己决定「这不算问题」
        assert not hasattr(Status, "NOTICE")

    def test_checks_modules_do_not_use_notice(self):
        for path in (ROOT / "checks").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "Status.NOTICE" not in text, f"{path.name} 仍在用 NOTICE"

    def test_rustdesk_missing_server_is_warn(self):
        # 远程黑屏是功能不可用，必须报 WARN，不能自行放行
        src = (ROOT / "checks/rustdesk.py").read_text(encoding="utf-8")
        block = src[src.index("elif not has_server:"):]
        block = block[:block.index("elif not has_desktop:")]
        assert "Status.WARN" in block

    def test_hardware_acceptable_serial_is_warn(self):
        src = (ROOT / "checks/hardware.py").read_text(encoding="utf-8")
        block = src[src.index("elif serial in acceptable:"):]
        block = block[:block.index("else:")]
        assert "Status.WARN" in block

    def test_warn_still_reported_in_verdict(self):
        report = Report(host="x", user="u", started_at="t")
        report.add(_item(1, "RustDesk", Status.WARN))
        report.add(_item(2, "激光雷达", Status.PASS))
        assert report.verdict_class == "warn"
        assert len(report.warned) == 1


class TestManualAcceptInHtml:
    def test_warn_and_skip_get_checkbox_fail_does_not(self, tmp_path):
        html, _ = _render([
            _item(1, "网络连通性", Status.PASS),
            _item(2, "RustDesk", Status.WARN),
            _item(3, "运动控制", Status.SKIP),
            _item(4, "激光雷达", Status.FAIL),
        ], tmp_path)

        rows = re.findall(r'<tr data-number="(\d+)"[^>]*>', html)
        assert set(rows) == {"1", "2", "3", "4"}

        # 只有 2（WARN）和 3（SKIP）带 accept-chk
        keys = re.findall(r'class="accept-chk" data-key="r(\d+)"', html)
        assert set(keys) == {"2", "3"}
        # FAIL 项绝不出现确认框
        assert "r4" not in keys

    def test_pdf_and_print_buttons_present(self, tmp_path):
        html, _ = _render([_item(1, "网络", Status.PASS)], tmp_path)
        assert 'id="btn-pdf"' in html
        assert 'id="btn-print"' in html
        assert "下载 PDF 检测报告" in html
        assert "window.print()" in html

    def test_manual_checklist_present(self, tmp_path):
        html, _ = _render([_item(1, "网络", Status.PASS)], tmp_path)
        assert 'id="manual"' in html
        # 用户 2026-11-02 追加的两条
        assert "车与遥控器是否匹配" in html
        assert "车是否跑得直" in html

    def test_state_persisted_and_carried_to_pdf(self, tmp_path):
        html, _ = _render([_item(2, "RustDesk", Status.WARN)], tmp_path)
        # 勾选状态存 localStorage，打印时同一 DOM 天然带走
        assert "localStorage.setItem" in html
        assert "localStorage.getItem" in html
        # 打印样式必须保留颜色，否则 PDF 里看不错状态
        assert "print-color-adjust: exact" in html

    def test_review_log_section_exists(self, tmp_path):
        html, _ = _render([_item(1, "网络", Status.PASS)], tmp_path)
        assert 'id="review-log"' in html
        assert "程序完成自动检测" in html

    def test_no_notice_section_remains(self, tmp_path):
        html, md = _render([_item(1, "网络", Status.PASS)], tmp_path)
        assert "提示项（不影响出货）" not in html
        assert "提示项（不影响出货）" not in md

    def test_md_reports_raw_status(self, tmp_path):
        _, md = _render([
            _item(2, "RustDesk", Status.WARN, "仅 --service 在运行"),
        ], tmp_path)
        assert "WARN" in md
        # md 里提示「可在 html 里确认接受」
        assert "确认接受" in md


class TestCleanupIsOptIn:
    def test_auto_cleanup_defaults_to_false(self):
        import yaml
        cfg = yaml.safe_load((ROOT / "products/tianracer_t110_rdk/baseline.yaml").read_text(encoding="utf-8"))
        assert cfg["traces"]["auto_cleanup"] is False

    def test_keep_bash_history_defaults_to_true(self):
        import yaml
        cfg = yaml.safe_load((ROOT / "products/tianracer_t110_rdk/baseline.yaml").read_text(encoding="utf-8"))
        assert cfg["traces"]["keep_bash_history"] is True

    def test_script_keeps_history_by_default(self):
        from checks.traces import build_clean_script
        script = build_clean_script(keep_bash_history=True)
        assert ': > "$H/.bash_history"' not in script
        assert "KEPT_HISTORY=1" in script

    def test_script_can_clear_history_when_asked(self):
        from checks.traces import build_clean_script
        script = build_clean_script(keep_bash_history=False)
        assert ': > "$H/.bash_history"' in script
        assert "KEPT_HISTORY=0" in script

    def test_cleanup_never_touches_git(self):
        # git 历史属于仓库，任何情况下都不能被清理脚本动
        from checks.traces import build_clean_script
        for keep in (True, False):
            script = build_clean_script(keep)
            assert "git " not in script


class TestVoltageSingleThreshold:
    def test_baseline_is_single_threshold(self):
        import yaml
        cfg = yaml.safe_load((ROOT / "products/tianracer_t110_rdk/baseline.yaml").read_text(encoding="utf-8"))
        from core.verdict import is_single_threshold
        assert is_single_threshold(cfg["voltage"]["normal_v"],
                                   cfg["voltage"]["low_v"])

    def test_single_threshold_has_no_warn_path(self):
        from core.verdict import judge_voltage
        # 11.0 是交付线，等于通过；低一点点就 FAIL，中间没有 WARN 档
        assert judge_voltage(11.0, 11.0, 11.0) is Status.PASS
        assert judge_voltage(10.99, 11.0, 11.0) is Status.FAIL

    def test_dual_threshold_still_warns(self):
        from core.verdict import judge_voltage
        # 若把 normal_v 调到 11.7，「偏低提醒」档恢复
        assert judge_voltage(11.3, 11.7, 11.0) is Status.WARN
        assert judge_voltage(11.7, 11.7, 11.0) is Status.PASS
