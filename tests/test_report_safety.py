"""报告渲染的安全与语义测试。

覆盖 Round 7 的三类反馈：

  M5  fatal 项停在 PENDING 不能判「可交付」
  建议修 报告 HTML 必须转义（车辆命令输出会插进去）
  建议修 PDF 文件名里的 host 必须消毒
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.verdict import CheckResult, Report, Status  # noqa: E402


def _report(statuses, **kwargs):
    report = Report(host=kwargs.pop("host", "10.0.0.9"),
                    user="sunrise", started_at="2026-11-05 10:00:00")
    for key, value in kwargs.items():
        setattr(report, key, value)
    for index, (status, fatal) in enumerate(statuses, start=1):
        report.add(CheckResult(number=index, title=f"项{index}",
                               status=status, summary="结论", fatal=fatal))
    return report


class TestFatalPending:
    """致命项没做完落地互校，整车不能说「可交付」。

    IMU / 里程计在架空段只能测频率与静态指标，航向精度要落地转圈
    才有结论。那之前它们是 PENDING —— 既不是通过也不是失败。
    如果照常放行，「致命项没验证完」就被 skip_ratio 的 20% 阈值盖过去了。
    """

    def test_fatal_pending_blocks_delivery(self):
        report = _report([(Status.PASS, False)] * 17
                         + [(Status.PENDING, True), (Status.PENDING, True)])
        assert report.verdict.startswith("不具备交付条件")
        assert "落地互校" in report.verdict
        assert report.verdict_class == "bad"

    def test_fatal_pass_is_deliverable(self):
        report = _report([(Status.PASS, False)] * 17
                         + [(Status.PASS, True), (Status.PASS, True)])
        assert report.verdict == "具备交付条件"

    def test_non_fatal_pending_only_warns(self):
        report = _report([(Status.PASS, False)] * 17
                         + [(Status.PENDING, False), (Status.PENDING, False)])
        assert report.verdict.startswith("可交付")
        assert "待落地验证" in report.verdict

    def test_fatal_fail_still_takes_priority(self):
        """FAIL 的措辞优先于 PENDING —— 别让两种原因混在一句里。"""
        report = _report([(Status.PASS, False)] * 17
                         + [(Status.FAIL, True), (Status.PENDING, True)])
        # 致命项 FAIL 有专门的措辞，优先于 PENDING
        assert report.verdict == "不具备交付条件（存在致命项失败）"


class TestHtmlEscaping:
    """HTML 必须转义，Markdown 必须不转义。"""

    @staticmethod
    def _render(report, tmp_path):
        from core.reporting import render
        md, html = render(report, tmp_path)
        return (md.read_text(encoding="utf-8"),
                html.read_text(encoding="utf-8"))

    def test_script_in_summary_is_escaped(self, tmp_path):
        report = Report(host="10.0.0.9", user="u",
                        started_at="2026-11-05 10:00:00")
        report.add(CheckResult(
            number=1, title="网络", status=Status.PASS,
            summary='<script>alert("xss")</script>',
        ))
        _md, html = self._render(report, tmp_path)

        assert '<script>alert("xss")</script>' not in html
        assert "&lt;script&gt;" in html

    def test_img_onerror_is_escaped(self, tmp_path):
        report = Report(host="10.0.0.9", user="u",
                        started_at="2026-11-05 10:00:00")
        report.add(CheckResult(
            number=1, title="网络", status=Status.PASS, summary="ok",
            detail="<img src=x onerror=alert(1)>",
        ))
        _md, html = self._render(report, tmp_path)

        assert "<img src=x onerror" not in html
        assert "&lt;img" in html

    def test_measurement_value_is_escaped(self, tmp_path):
        """测量值也来自车辆输出（如 frame_id），同样要转义。"""
        report = Report(host="10.0.0.9", user="u",
                        started_at="2026-11-05 10:00:00")
        item = CheckResult(number=1, title="TF", status=Status.PASS,
                           summary="ok")
        item.add("<b>frame</b>", "<i>x</i>")
        report.add(item)
        _md, html = self._render(report, tmp_path)

        assert "<b>frame</b>" not in html
        assert "<i>x</i>" not in html

    def test_markdown_keeps_raw_angle_brackets(self, tmp_path):
        """Markdown 不能转义：`<` 在那里是普通字符。"""
        report = Report(host="10.0.0.9", user="u",
                        started_at="2026-11-05 10:00:00")
        report.add(CheckResult(
            number=1, title="网络", status=Status.PASS,
            summary="值 < 阈值",
        ))
        md, _html = self._render(report, tmp_path)

        assert "值 < 阈值" in md
        assert "&lt;" not in md

    def test_normal_content_unaffected(self, tmp_path):
        """正常中文与数字不能被转义坏。"""
        report = Report(host="10.0.0.9", user="u",
                        started_at="2026-11-05 10:00:00")
        report.add(CheckResult(number=1, title="网络", status=Status.PASS,
                               summary="SSH 正常，延迟 111 ms"))
        _md, html = self._render(report, tmp_path)
        assert "SSH 正常，延迟 111 ms" in html


class TestPdfFilenameSanitising:
    def test_host_with_html_is_sanitised(self, tmp_path):
        """PDF 文件名直接拼进 JS 字符串，必须只留安全字符。"""
        from core.reporting import render

        report = Report(host='1.2.3.4"><script>alert(1)</script>',
                        user="u", started_at="2026-11-05 10:00:00")
        report.add(CheckResult(number=1, title="网络", status=Status.PASS,
                               summary="ok"))
        _md, html_path = render(report, tmp_path)
        html = html_path.read_text(encoding="utf-8")

        # 文件名行里不能出现引号或尖括号
        import re
        match = re.search(r"var PDF_FILENAME = '检测报告_([^_]+)_", html)
        assert match, "没找到 PDF_FILENAME"
        name_part = match.group(1)
        assert not set(name_part) & set('"<>/\\\'')
        assert "script" in name_part    # 字母被保留（无害）

    def test_normal_ip_unchanged(self, tmp_path):
        from core.reporting import render

        report = Report(host="192.168.128.10", user="u",
                        started_at="2026-11-05 10:00:00")
        report.add(CheckResult(number=1, title="网络", status=Status.PASS,
                               summary="ok"))
        _md, html_path = render(report, tmp_path)
        assert "检测报告_192.168.128.10_" in html_path.read_text(encoding="utf-8")


class TestSpinSafetyLimitsConsistency:
    """baseline 的转圈上限不能比探针里的硬编码更宽 —— 否则收紧是假的。"""

    def test_baseline_spin_limits_not_looser_than_probe(self):
        import re
        from pathlib import Path as _P
        from tests._fixtures import get_profile

        probe = (_P(__file__).resolve().parent.parent
                 / "probes" / "spin_probe.py").read_text(encoding="utf-8")
        hard_speed = float(re.search(r"HARD_MAX_SPEED = ([\d.]+)", probe).group(1))
        hard_seconds = float(re.search(r"HARD_MAX_SECONDS = ([\d.]+)", probe).group(1))

        safety = get_profile().baseline["motion"]["safety"]
        assert safety["spin_max_speed_mps"] <= hard_speed, (
            f"baseline 允许 {safety['spin_max_speed_mps']} m/s，"
            f"但探针硬上限是 {hard_speed} —— 配置比保护更宽，收紧无效"
        )
        assert safety["spin_max_seconds"] <= hard_seconds, (
            f"baseline 允许 {safety['spin_max_seconds']} s，"
            f"但探针硬上限是 {hard_seconds}"
        )

class TestGroundResolutionPlacement:
    """回填 PENDING 必须在 finally 里。

    若运动阶段抛异常或 Ctrl-C，这些项不能停在
    「航向精度待落地互校」—— 那读起来像「还差一步就能过」，
    实际是根本没验证。结论不受影响（已 aborted_reason），
    但报告措辞必须准确。
    """

    def test_resolve_is_in_finally_not_try(self):
        import ast
        from pathlib import Path as _P

        src = (_P(__file__).resolve().parent.parent
               / "core" / "runner.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        # 直接在整个文件里找 _run_inner 的函数定义节点
        fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_run_inner":
                fn = node
                break
        assert fn is not None, "没找到 _run_inner"

        # 只看 _run_inner 自己的那个最外层 try —— 它才是决定
        # 「异常路径走不走得到」的那一层。内层 try/except 只是
        # 局部兜底（包住本次调用），不影响结论。
        outer_try = None
        for node in fn.body:
            if isinstance(node, ast.Try) and node.finalbody:
                outer_try = node
                break
        assert outer_try is not None, "_run_inner 里没有带 finally 的 try"

        def _contains(nodes):
            for sub in ast.walk(ast.Module(body=nodes, type_ignores=[])):
                if (isinstance(sub, ast.Attribute)
                        and sub.attr == "_resolve_ground_dependents"):
                    return True
            return False

        in_finally = _contains(outer_try.finalbody)
        in_try = _contains(outer_try.body)

        assert in_finally, "回填逻辑不在 finally 里，异常路径不会执行"
        assert not in_try, "回填逻辑不该在 try 主体里（正常路径已移除）"


class TestSpinUnreachedIsSkip:
    """没转够一圈记 SKIP（未验证），不记 WARN（有偏差）。

    两者在报告里的含义完全不同：WARN 读作「测了但有偏差」，
    SKIP 才是「没测到」。转不够圈属于后者。
    """

    def test_unreached_without_anomaly_is_not_skip_from_warn(self):
        import inspect
        from checks import motion

        src = inspect.getsource(motion._run_spin)
        # 定位 not reached 分支
        idx = src.find('if not data.get("reached")')
        assert idx >= 0, "没找到 reached 分支"
        branch = src[idx:idx + 600]
        assert "Status.SKIP" in branch, "该分支应记 SKIP"
        assert "Status.WARN" not in branch, "该分支不该记 WARN"

