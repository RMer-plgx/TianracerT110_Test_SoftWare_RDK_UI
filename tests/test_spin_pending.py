"""落地转向互校与中间态（PENDING）的测试。

背景（用户 2026-11-03）：
架空段测 IMU / 里程计只能验证「有数据、频率对、静止不漂」，
证明不了走起来以后航向准不准。要落地左转、右转各转一整圈，
比对里程计推算的航向与陀螺仪实测的航向。

并且：架空段一测完不能就标绿 —— 那等于把「测了一半」当成「测完了」。
需要一个中间态，等落地互校通过才转 PASS。

这组测试守四件事：
  1. PENDING 不算通过，且计入未验证比例
  2. IMU / odom 在有运动测试时标 PENDING，不做运动测试时标 PASS 并注明
  3. 互校判定按偏差大小分档，左右任一不合格即整体不合格
  4. 系统健康的措辞必须写出具体值与阈值
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._fixtures import attach_profile, get_profile  # noqa: E402
from core.verdict import (  # noqa: E402
    CheckResult, Report, Status,
)


def _report(*statuses) -> Report:
    report = Report(host="10.0.0.9", user="sunrise",
                    started_at="2026-11-03 10:00:00")
    for index, status in enumerate(statuses, start=1):
        report.add(CheckResult(number=index, title=f"项{index}",
                               status=status, summary="事实"))
    return report


class TestPendingSemantics:
    def test_pending_is_not_pass(self):
        # 核心约束：只测一半不等于验证过
        assert Status.PENDING.counts_as_pass is False

    def test_pending_marker_is_distinct(self):
        assert Status.PENDING.marker not in (
            Status.PASS.marker, Status.SKIP.marker, Status.WARN.marker)

    def test_pending_counts_as_unverified(self):
        report = _report(Status.PASS, Status.PENDING, Status.PASS)
        assert len(report.pending) == 1
        assert len(report.unverified) == 1
        # PENDING 没被算进通过
        assert len(report.passed) == 2

    def test_pending_affects_skip_ratio(self):
        # 1 PASS + 1 PENDING：未验证比例 50%，必须触发不合格
        report = _report(Status.PASS, Status.PENDING)
        assert report.skip_ratio == pytest.approx(0.5)

    def test_too_many_pending_blocks_delivery(self):
        report = _report(Status.PASS, *[Status.PENDING] * 4)
        assert report.verdict_class == "bad"
        assert "未验证比例" in report.verdict

    def test_verdict_mentions_pending_separately(self):
        report = _report(Status.PASS, Status.PENDING, Status.PASS,
                         Status.PASS, Status.PASS, Status.PASS,
                         Status.PASS, Status.PASS, Status.PASS,
                         Status.PASS, Status.PASS, Status.PASS)
        assert "待落地验证" in report.verdict
        assert report.verdict_class == "warn"

    def test_skip_and_pending_reported_differently(self):
        report = _report(Status.PASS, Status.PENDING, Status.SKIP,
                         *[Status.PASS] * 9)
        assert len(report.pending) == 1
        assert len(report.skipped) == 1
        assert len(report.unverified) == 2


class _ProbeRecorder:
    """记录 run_probe 的调用，并按预设返回数据。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, ctx, probe_name, *args, timeout=90, needs_ros=True):
        self.calls.append((probe_name, args))
        if not self.responses:
            return None
        return self.responses.pop(0)


class _FakeCtx:
    def __init__(self, baseline=None, motion_level="full"):
        self.profile = attach_profile(self).profile
        self.baseline = baseline or {}
        self.motion_level = motion_level
        self.cancel_requested = False
        self.voltage_stop = False
        self.voltage_known = True
        self.voltage_v = 11.9
        self.motion_armed = True
        self.force_motion = False
        self.say = lambda text: None
        self.spin_verified = False


SPIN_OK = {
    "ok": True, "aborted": False, "reached": True, "anomaly": "",
    "target_deg": 360, "imu_yaw_deg": 358.2, "odom_yaw_deg": 351.7,
    "yaw_diff_deg": 6.5, "closure_m": 0.12, "duration": 36.4,
    "samples": 730, "imu_source": "orientation",
}


def _spin(imu, odom, reached=True, aborted=False):
    data = dict(SPIN_OK)
    data["imu_yaw_deg"] = imu
    data["odom_yaw_deg"] = odom
    data["yaw_diff_deg"] = abs(imu - odom)
    data["reached"] = reached
    data["aborted"] = aborted
    return data


class TestSpinJudgement:
    """转向互校的判定分档与「左右取最差」。"""

    BASELINE = {
        "motion": {
            "spin": {
                "speed": 0.12, "steer_angle": 0.35, "target_yaw_deg": 360,
                "max_seconds": 90, "yaw_tolerance_deg": 15,
                "yaw_warn_deg": 30, "closure_warn_m": 0.30,
            },
            "safety": {"spin_max_speed_mps": 0.25, "spin_max_seconds": 120},
        }
    }

    def _run(self, monkeypatch, responses, motion_level="full"):
        from checks import motion

        recorder = _ProbeRecorder(responses)
        monkeypatch.setattr(motion, "run_probe", recorder)
        ctx = _FakeCtx(self.BASELINE, motion_level)
        result = CheckResult(number=18, title="运动控制", status=Status.PASS)
        statuses, verified = motion._run_spin(
            ctx, self.BASELINE["motion"], result, self.BASELINE["motion"]["safety"])
        return statuses, verified, result, recorder

    def test_small_diff_passes_and_verifies(self, monkeypatch):
        statuses, verified, result, _ = self._run(
            monkeypatch, [_spin(360.0, 355.0), _spin(-360.0, -356.0)])
        assert all(s is Status.PASS for s in statuses)
        assert verified is True

    def test_medium_diff_warns_and_stays_unverified(self, monkeypatch):
        # 差 20° 在 15~30 之间：告警，且不能算验证通过
        statuses, verified, _, _ = self._run(
            monkeypatch, [_spin(360.0, 340.0), _spin(-360.0, -340.0)])
        assert any(s is Status.WARN for s in statuses)
        assert verified is False

    def test_large_diff_fails(self, monkeypatch):
        statuses, verified, _, _ = self._run(
            monkeypatch, [_spin(360.0, 300.0), _spin(-360.0, -300.0)])
        assert any(s is Status.FAIL for s in statuses)
        assert verified is False

    def test_one_side_bad_blocks_verification(self, monkeypatch):
        # 左转好、右转差 —— 不能因为一侧通过就放行
        statuses, verified, _, _ = self._run(
            monkeypatch, [_spin(360.0, 358.0), _spin(-360.0, -300.0)])
        assert verified is False

    def test_unreached_spin_does_not_verify(self, monkeypatch):
        # 没转到 360°（异常提前停）不能算验证过
        statuses, verified, _, _ = self._run(
            monkeypatch, [_spin(120.0, 118.0, reached=False),
                          _spin(-360.0, -358.0)])
        assert verified is False

    def test_aborted_spin_does_not_verify(self, monkeypatch):
        statuses, verified, _, _ = self._run(
            monkeypatch, [_spin(100.0, 99.0, reached=False, aborted=True),
                          _spin(-360.0, -358.0)])
        assert verified is False

    def test_probe_failure_fails(self, monkeypatch):
        statuses, verified, _, _ = self._run(monkeypatch, [None, _spin(-360, -358)])
        assert any(s is Status.FAIL for s in statuses)
        assert verified is False

    def test_left_and_right_both_probed(self, monkeypatch):
        _, _, _, recorder = self._run(
            monkeypatch, [_spin(360.0, 358.0), _spin(-360.0, -358.0)])
        assert [c[0] for c in recorder.calls] == ["spin_probe", "spin_probe"]
        # 左右的目标角度符号必须相反
        targets = [float(c[1][2]) for c in recorder.calls]
        assert targets[0] > 0 > targets[1]

    def test_speed_clamped_to_safety_limit(self, monkeypatch):
        """配置里的速度只能被收紧，不能被放宽。"""
        baseline = {
            "motion": {
                "spin": {"speed": 5.0, "steer_angle": 0.35,
                         "target_yaw_deg": 360, "max_seconds": 90},
                "safety": {"spin_max_speed_mps": 0.25, "spin_max_seconds": 120},
            }
        }
        from checks import motion
        recorder = _ProbeRecorder([_spin(360, 358), _spin(-360, -358)])
        monkeypatch.setattr(motion, "run_probe", recorder)
        ctx = _FakeCtx(baseline, "full")
        result = CheckResult(number=18, title="运动控制", status=Status.PASS)
        motion._run_spin(ctx, baseline["motion"], result,
                         baseline["motion"]["safety"])
        assert float(recorder.calls[0][1][0]) <= 0.25


class TestImuOdomPending:
    def _run_check(self, module_name, ctx):
        import importlib
        module = importlib.import_module(f"checks.{module_name}")
        return module.run(ctx)

    def test_imu_pending_when_motion_enabled(self, monkeypatch):
        from checks import imu
        monkeypatch.setattr(imu, "measure_hz", lambda *a, **k: 50.0)
        monkeypatch.setattr(imu, "run_probe", lambda *a, **k: {
            "ok": True,
            "accel": {"x": 0.1, "y": 0.1, "z": 9.8},
            "accel_norm": 9.8,
            "gyro": {"x": 0.001, "y": 0.001, "z": 0.001},
            "roll_deg": 0.5, "pitch_deg": 0.5,
        })
        ctx = _FakeCtx({}, motion_level="full")
        result = imu.run(ctx)
        assert result.status is Status.PENDING
        assert result.needs_ground is True

    def test_imu_pass_when_motion_disabled(self, monkeypatch):
        from checks import imu
        monkeypatch.setattr(imu, "measure_hz", lambda *a, **k: 50.0)
        monkeypatch.setattr(imu, "run_probe", lambda *a, **k: {
            "ok": True,
            "accel": {"x": 0.1, "y": 0.1, "z": 9.8},
            "accel_norm": 9.8,
            "gyro": {"x": 0.001, "y": 0.001, "z": 0.001},
            "roll_deg": 0.5, "pitch_deg": 0.5,
        })
        ctx = _FakeCtx({}, motion_level="none")
        result = imu.run(ctx)
        # 不做运动测试时，落地段本来就不在范围内 → PASS，但要注明
        assert result.status is Status.PASS
        assert result.needs_ground is False
        assert "未验证" in result.summary

    def test_odom_pending_when_motion_enabled(self, monkeypatch):
        from checks import odom
        monkeypatch.setattr(odom, "run_probe", lambda *a, **k: {
            "ok": True, "hz": 100.0, "static_drift_m": 0.0,
            "start": {"x": 0, "y": 0, "z": 0}, "end": {"x": 0, "y": 0, "z": 0},
        })
        ctx = _FakeCtx({"odom": {"sample_seconds": 5}}, motion_level="bench")
        result = odom.run(ctx)
        assert result.status is Status.PENDING
        assert result.needs_ground is True


class TestHealthWording:
    """系统健康：通过要列三个值，未通过要点名哪项超多少。"""

    BASELINE = {"health": {
        "temp_warn_c": 75.0, "temp_fail_c": 85.0,
        "disk_warn_pct": 85.0, "disk_fail_pct": 95.0,
        "load_warn": 3.0, "mem_available_min_gib": 0.5,
    }}

    class _Out:
        def __init__(self, stdout):
            self.stdout = stdout
            self.stderr = ""
            self.ok = True

    def _ctx(self, load, temp, disk=67, mem=6):
        outer = self

        class _Remote:
            def run(self, cmd, **kwargs):
                if "thermal" in cmd:
                    return outer._Out(f"x {int(temp * 1000)}\n")
                if "loadavg" in cmd:
                    return outer._Out(f"{load} 1.5 1.7 2/300 12345\n")
                if "df -h" in cmd:
                    return outer._Out(f"/dev/root 30G 20G 10G {disk}% /\n")
                if "free -g" in cmd:
                    return outer._Out(f"8 {mem}\n")
                return outer._Out("")

        class _Ctx:
            profile = get_profile()
            remote = _Remote()
            baseline = outer.BASELINE
        return _Ctx()

    def test_pass_lists_all_three_values(self):
        from checks import health
        result = health.run(self._ctx(1.26, 40.0, 67))
        assert result.status is Status.PASS
        assert "温度" in result.summary and "40.0" in result.summary
        assert "磁盘" in result.summary and "67%" in result.summary
        assert "负载" in result.summary and "1.26" in result.summary

    def test_warn_names_item_and_threshold(self):
        from checks import health
        result = health.run(self._ctx(3.5, 40.0, 67))
        assert result.status is Status.WARN
        assert "负载 3.50" in result.summary
        assert "超过警告线 3.0" in result.summary

    def test_fail_names_item_and_fail_threshold(self):
        from checks import health
        result = health.run(self._ctx(1.0, 90.0, 67))
        assert result.status is Status.FAIL
        assert "温度 90.0" in result.summary
        assert "超过失败线 85" in result.summary

    def test_multiple_bad_items_all_listed(self):
        from checks import health
        result = health.run(self._ctx(3.5, 80.0, 90))
        assert result.status is Status.WARN
        assert "温度" in result.summary
        assert "磁盘" in result.summary
        assert "负载" in result.summary

    def test_low_values_never_warn(self):
        # 单侧指标：低值不该告警（冬天冷启动、空载）
        from checks import health
        result = health.run(self._ctx(0.2, 5.0, 30))
        assert result.status is Status.PASS

class TestYawAccumulation:
    """转圈探针的航向累积数学。

    这是最容易算错的地方：转完一圈回到 0°，
    如果用「末值 - 初值」算，结果是 0 而不是 360。
    必须逐样本累积增量，并处理 ±180° 折叠。
    """

    @staticmethod
    def _load():
        import math
        import sys
        import types

        # 探针顶部 import rospy 与 ROS 消息类型，这里用空壳顶替，
        # 只为拿到纯数学部分（YawTracker）。
        for name in ("rospy", "ackermann_msgs", "ackermann_msgs.msg",
                     "nav_msgs", "nav_msgs.msg", "sensor_msgs", "sensor_msgs.msg"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["ackermann_msgs.msg"].AckermannDrive = type("AckermannDrive", (), {})
        sys.modules["nav_msgs.msg"].Odometry = type("Odometry", (), {})
        sys.modules["sensor_msgs.msg"].Imu = type("Imu", (), {})

        from core.paths import resource_path
        src = resource_path("probes/spin_probe.py").read_text(encoding="utf-8")
        head = src.split("def publish(")[0]
        ns = {"__name__": "spinmod"}
        exec(compile(head, "spin_probe", "exec"), ns)
        return ns["YawTracker"], math

    def _simulate(self, delta_deg, step_deg=5.0, start_deg=0.0):
        YawTracker, math = self._load()
        tracker = YawTracker()
        cur = math.radians(start_deg)
        tracker.update(cur)
        steps = int(abs(delta_deg) / step_deg)
        step = math.radians(step_deg) * (1 if delta_deg > 0 else -1)
        for _ in range(steps):
            cur = (cur + step + math.pi) % (2 * math.pi) - math.pi
            tracker.update(cur)
        return tracker.degrees

    def test_full_turn_left_is_360(self):
        assert abs(self._simulate(360) - 360) < 1

    def test_full_turn_right_is_minus_360(self):
        assert abs(self._simulate(-360) + 360) < 1

    def test_full_turn_across_wrap(self):
        # 从 179° 起步，中途会跨过 ±180° 折叠
        assert abs(self._simulate(360, start_deg=179) - 360) < 1

    def test_full_turn_across_wrap_negative(self):
        assert abs(self._simulate(-360, start_deg=-179) + 360) < 1

    def test_small_turn(self):
        assert abs(self._simulate(30) - 30) < 0.5

    def test_more_than_one_turn(self):
        # 允许略超一圈（判定用 1.3 倍才异常）
        assert abs(self._simulate(370) - 370) < 1

class TestGroundDependentsResolution:
    """runner 在落地段结束后回填「待落地验证」的项。

    链路：架空段 → IMU/odom 标 PENDING → 落地转圈互校 → 回填。
    这段逻辑横跨 runner 与 motion，必须有集成测试守住。
    """

    @staticmethod
    def _runner():
        from core.runner import Runner
        runner = Runner(host="10.0.0.9", user="u", password="p")
        runner.record_dir = None
        return runner

    @staticmethod
    def _ctx(verified):
        class _Ctx:
            profile = get_profile()
            spin_verified = verified
            def say(self, text):
                pass
        return _Ctx()

    def test_verified_turns_pending_into_pass(self):
        from core.verdict import CheckResult, Status

        runner = self._runner()
        imu = CheckResult(number=8, title="IMU", status=Status.PENDING,
                          needs_ground=True,
                          summary="50 Hz；频率与静态指标正常，航向精度待落地互校")
        runner.report.add(imu)
        runner._resolve_ground_dependents(self._ctx(True))

        assert imu.status is Status.PASS
        assert "已由落地转向互校确认" in imu.summary

    def test_not_verified_stays_pending(self):
        from core.verdict import CheckResult, Status

        runner = self._runner()
        odom = CheckResult(number=12, title="里程计", status=Status.PENDING,
                           needs_ground=True,
                           summary="100 Hz；频率与静止漂移正常，航向精度待落地互校")
        runner.report.add(odom)
        runner._resolve_ground_dependents(self._ctx(False))

        assert odom.status is Status.PENDING
        assert "未验证" in odom.summary

    def test_no_affected_items_is_noop(self):
        from core.verdict import CheckResult, Status

        runner = self._runner()
        plain = CheckResult(number=1, title="网络", status=Status.PASS)
        runner.report.add(plain)
        runner._resolve_ground_dependents(self._ctx(True))
        assert plain.status is Status.PASS

    def test_both_items_resolved_together(self):
        from core.verdict import CheckResult, Status

        runner = self._runner()
        for number, title in ((8, "IMU"), (12, "里程计")):
            runner.report.add(CheckResult(
                number=number, title=title, status=Status.PENDING,
                needs_ground=True, summary="数据正常，航向精度待落地互校"))
        runner._resolve_ground_dependents(self._ctx(True))
        assert all(r.status is Status.PASS for r in runner.report.results)

