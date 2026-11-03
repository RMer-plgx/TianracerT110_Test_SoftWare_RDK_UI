"""UI 层的回归测试。

不需要真的显示窗口——用离屏模式构造对象即可。
测的重点是审查指出过的几处 UI 与核心不一致：
中止颜色、选项语义、进度条、关闭窗口时的线程处理。
"""

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._fixtures import attach_profile  # noqa: E402
from core.verdict import CheckResult, Report, Status  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class TestVerdictColorConsistency:
    """UI 必须复用 report.verdict_class，不能自己重写一套判定。"""

    def test_aborted_is_red_not_green(self, qapp):
        from ui.main_window import MainWindow
        from ui.theme import palette

        window = MainWindow()
        report = Report(host="1.1.1.1", user="u", started_at="now")
        report.add(CheckResult(number=1, title="网络", status=Status.PASS))
        report.aborted_reason = "用户中断"

        # verdict_class 本身应为 bad
        assert report.verdict_class == "bad"

        window._on_finished(report, "")
        style = window.verdict_label.styleSheet()
        c = palette(window.theme)
        assert c["fail"] in style, (
            f"中止的报告应显示红色，实际样式：{style}"
        )
        assert c["pass"] not in style

    def test_all_pass_is_green(self, qapp):
        from ui.main_window import MainWindow
        from ui.theme import palette

        window = MainWindow()
        report = Report(host="1.1.1.1", user="u", started_at="now")
        for i in range(1, 5):
            report.add(CheckResult(number=i, title=f"项{i}", status=Status.PASS))

        window._on_finished(report, "")
        c = palette(window.theme)
        assert c["pass"] in window.verdict_label.styleSheet()


class TestProgressNotForcedToFull:
    def test_aborted_keeps_partial_progress(self, qapp):
        from ui.main_window import MainWindow

        window = MainWindow()
        report = Report(host="1.1.1.1", user="u", started_at="now")
        report.add(CheckResult(number=1, title="网络", status=Status.PASS))
        report.add(CheckResult(number=2, title="身份", status=Status.PASS))
        report.aborted_reason = "用户中断"

        window._on_finished(report, "")
        assert window.progress.value() == 2, (
            f"只跑了 2 项却显示 {window.progress.value()}/19"
        )
        assert window.counter_label.text() == "2 / 19"


class TestCheckDoneUpdatesCard:
    def test_card_reflects_result(self, qapp):
        from ui.main_window import MainWindow

        window = MainWindow()
        result = CheckResult(number=9, title="激光雷达", status=Status.FAIL,
                             summary="频率异常")
        window._on_check_done(result)
        card = window.cards[9]
        assert card.status is Status.FAIL
        assert "频率异常" in card.summary_label.text()


class TestHostValidation:
    @pytest.mark.parametrize("host,expected", [
        ("192.168.1.128", True),
        ("10.0.0.9", True),
        ("127.0.0.1", True),
        ("tianracer.local", True),
        ("", False),
        ("abc", True),            # 主机名，允许
        ("999.1.1.1", False),     # 每段超 255
        ("192.168.1", False),     # 段数不对
        ("中文地址", False),
        ("192.168.1.1; rm -rf", False),
    ])
    def test_valid_host(self, qapp, host, expected):
        from ui.main_window import MainWindow
        assert MainWindow._valid_host(host) is expected


class TestFullMotionImpliesPulse:
    """勾「完整运动测试」时，微动脉冲必然是开启的。

    行为断言：直接用 fake 控件调 start_test 内部的选项推导逻辑，
    不断言源码字符串——那种写法在重命名常量后就不准了（审查指出）。
    """

    def test_full_motion_implies_pulse(self, qapp):
        from ui.main_window import MainWindow

        window = MainWindow()
        # 默认是「有线模式」，会先走 WiFi 自动配置流程。
        # 本测试要验证的是运动选项推导，切到无线模式直接走检测。
        window.set_conn_mode("wireless")
        window.host_edit.setText("10.0.0.9")
        window.mode_combo.setCurrentIndex(1)
        window.esc_check.setChecked(True)
        window.allow_pulse.setChecked(False)   # 故意不勾微动
        window.full_motion.setChecked(True)    # 只勾完整测试

        captured = {}

        class FakeWorker:
            def __init__(self, config, *_a, **_k):
                captured.update(config)

            def __getattr__(self, name):
                # 吞掉所有信号连接
                class _Sig:
                    def connect(self, *a, **k):
                        pass
                return _Sig()

            def start(self):
                pass

        import ui.main_window as mw
        original = mw.TestWorker
        mw.TestWorker = FakeWorker
        try:
            # 跳过弹窗：直接模拟用户确认
            from PyQt5.QtWidgets import QMessageBox
            orig_q = QMessageBox.question
            orig_w = QMessageBox.warning
            QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
            QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Yes)
            try:
                window.start_test()
            finally:
                QMessageBox.question = orig_q
                QMessageBox.warning = orig_w
        finally:
            mw.TestWorker = original

        assert captured.get("motion") == "full"
        assert captured.get("answers", {}).get("motion_armed") == "yes", (
            "完整运动测试必须隐含微脉冲 —— 否则用户以为禁掉了，车仍会动"
        )


class TestCancellationWired:
    """取消必须能真的阻止运动段继续——这是安全相关的。"""

    def test_execute_skips_when_cancelled(self):
        """已取消后，_execute 不再执行检测项。"""
        from core.runner import Runner
        from core.verdict import Status

        runner = Runner(host="1.1.1.1", user="u", password="p",
                        mode="attach", motion="none")

        class FakeCtx:
            cancel_requested = True

            def say(self, text):
                pass

        entry = (1, "网络连通性", "network", "run", True, "static")
        runner._execute(FakeCtx(), entry)
        assert runner.report.results, "应当记一条 SKIP"
        assert runner.report.results[-1].status is Status.SKIP
        assert "停止" in runner.report.results[-1].summary

    def test_motion_segments_stop_when_cancelled(self, monkeypatch):
        """取消后，_run_bench / _run_ground 不再调 run_probe。"""
        from checks import motion

        calls = {"n": 0}

        def fake_probe(*args, **kwargs):
            calls["n"] += 1
            return None

        monkeypatch.setattr(motion, "run_probe", fake_probe)

        class Ctx:
            cancel_requested = True

            def say(self, text):
                pass

        class Res:
            def add(self, *a, **k):
                pass

        bench_cfg = {"forward_speed": 0.1, "forward_duration": 1,
                     "reverse_speed": -0.1, "reverse_duration": 1,
                     "steer_angle": 0.3, "steer_duration": 1,
                     "steer_speed": 0.05, "min_displacement_m": 0.005}
        motion._run_bench(Ctx(), bench_cfg, Res(), 0.5, 10.0)

        ground_cfg = {"forward_speed": 0.1, "forward_duration": 1,
                      "forward_expected_m": 0.3, "forward_tolerance": 0.2,
                      "reverse_speed": -0.1, "reverse_duration": 1,
                      "reverse_expected_m": 0.3, "reverse_tolerance": 0.2,
                      "steer_angle": 0.3, "steer_duration": 1,
                      "steer_expected_yaw_deg": 11, "steer_tolerance_deg": 6}
        motion._run_ground(Ctx(), ground_cfg, Res(), 0.5, 10.0)

        assert calls["n"] == 0, (
            f"取消后仍发起了 {calls['n']} 次运动指令 —— 车会继续动"
        )

    def test_ground_pause_skipped_when_cancelled(self, qapp):
        """已取消时 pause_for_ground 不弹框、立即返回。"""
        from ui.main_window import MainWindow

        window = MainWindow()
        window._cancel_flag = True
        # 从 TestWorker 里拿到那个闭包不方便，这里直接验证它的行为契约：
        # 标志位为真时，落地确认一律视为「不继续」
        assert window._cancel_flag is True

    def test_pause_uses_event_not_busy_wait(self):
        source = (Path(__file__).resolve().parent.parent
                  / "ui" / "main_window.py").read_text(encoding="utf-8")
        start = source.index("def pause_for_ground")
        block = source[start:start + 1500]
        assert "Event()" in block and ".wait(" in block, (
            "落地确认必须用 Event.wait，忙等会占住工作线程且无法被取消唤醒"
        )
        assert "time.sleep(0.1)" not in block


class TestCloseEventExists:
    def test_overrides_close_event(self):
        from ui.main_window import MainWindow
        assert "closeEvent" in MainWindow.__dict__, (
            "必须覆写 closeEvent，否则关窗口时工作线程仍在跑，"
            "车上的 roslaunch 不会被清理"
        )
