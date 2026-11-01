"""路径与报告渲染测试。

重点验证两件在 Windows 上很容易踩的事：
1. 目录名不能含冒号
2. 中文报告必须显式 UTF-8 编码
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.paths import _safe_host, _safe_timestamp, records_root, resource_path
from core.verdict import CheckResult, Report, Status


class TestTimestamp:
    def test_no_colon(self):
        # Windows 路径非法字符
        assert ":" not in _safe_timestamp()

    def test_no_space(self):
        assert " " not in _safe_timestamp()

    def test_parseable(self):
        stamp = _safe_timestamp(datetime(2026, 10, 30, 17, 15, 0))
        assert stamp == "20261030_171500"

    def test_midnight(self):
        stamp = _safe_timestamp(datetime(2026, 1, 1, 0, 0, 0))
        assert stamp == "20260101_000000"


class TestSafeHost:
    def test_ip_is_fine(self):
        assert _safe_host("192.168.1.128") == "192.168.1.128"

    def test_strips_illegal_chars(self):
        assert ":" not in _safe_host("fe80::1")
        assert "/" not in _safe_host("a/b")

    def test_empty_becomes_unknown(self):
        assert _safe_host("") == "unknown"


class TestRecordsRoot:
    def test_name_is_test_records(self):
        assert records_root().name == "test records"

    def test_is_sibling_of_app_dir(self):
        """test records 必须与程序目录同级，不是它内部。"""
        from core.paths import app_dir
        assert records_root().parent == app_dir().parent

    def test_not_inside_app_dir(self):
        from core.paths import app_dir
        assert app_dir() not in list(records_root().parents)

    def test_containing_dir_is_workspace_parent(self):
        # records_root = <workspace>/test records
        # app_dir       = <workspace>/TianracerT110_Test_SoftWare_RDK
        from core.paths import app_dir
        assert records_root().parent == app_dir().parent


class TestResourcePath:
    def test_config_exists(self):
        assert resource_path("config/baseline.yaml").exists()

    def test_templates_exist(self):
        assert resource_path("report/md.j2").exists()
        assert resource_path("report/html.j2").exists()

    def test_probes_exist(self):
        for name in ("imu_probe.py", "lidar_probe.py", "odom_probe.py",
                     "tf_probe.py", "motion_probe.py"):
            assert resource_path(f"probes/{name}").exists(), name


class TestRender:
    def _sample_report(self):
        report = Report(host="192.168.1.128", user="sunrise",
                        started_at="2026-10-30 17:15:00",
                        finished_at="2026-10-30 17:28:00")
        report.add(CheckResult(number=1, title="网络连通性", status=Status.PASS,
                               summary="SSH 正常"))
        report.add(CheckResult(number=14, title="电池电压", status=Status.WARN,
                               summary="11.33 V 偏低"))
        report.answers["电调（ESC）是否已开启"] = "是"
        return report

    def test_render_writes_both_files(self, tmp_path):
        from core.reporting import render
        report = self._sample_report()
        md_path, html_path = render(report, tmp_path)

        assert md_path.exists() and html_path.exists()
        assert md_path.name == "report.md"
        assert html_path.name == "report.html"

    def test_markdown_is_utf8_with_chinese(self, tmp_path):
        from core.reporting import render
        md_path, _ = render(self._sample_report(), tmp_path)
        text = md_path.read_text(encoding="utf-8")
        assert "网络连通性" in text
        assert "192.168.1.128" in text
        assert "Tianracer" in text

    def test_html_has_verdict_class(self, tmp_path):
        from core.reporting import render
        _, html_path = render(self._sample_report(), tmp_path)
        text = html_path.read_text(encoding="utf-8")
        assert "verdict" in text
        assert 'charset="UTF-8"' in text

    def test_all_results_appear(self, tmp_path):
        from core.reporting import render
        md_path, _ = render(self._sample_report(), tmp_path)
        text = md_path.read_text(encoding="utf-8")
        assert "电池电压" in text
        assert "11.33 V 偏低" in text

    def test_skipped_section(self, tmp_path):
        from core.reporting import render
        report = self._sample_report()
        report.add(CheckResult(number=18, title="运动控制", status=Status.SKIP,
                               summary="未启用运动测试"))
        md_path, _ = render(report, tmp_path)
        text = md_path.read_text(encoding="utf-8")
        assert "未启用运动测试" in text
