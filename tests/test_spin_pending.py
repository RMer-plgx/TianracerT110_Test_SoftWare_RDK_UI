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


# 真实打滑：四驱同速的阿克曼车转弯时 odom 一定大于 IMU（转 360° 时约 29%）。
# 夹具用这个比例，否则「打滑率判据」测不出真实行为。
SPIN_OK = {
    "ok": True, "aborted": False, "reached": True, "anomaly": "",
    "target_deg": 360, "imu_yaw_deg": 360.5, "odom_yaw_deg": 465.0,
    "yaw_diff_deg": 104.5, "closure_m": 1.12, "duration": 49.8,
    "samples": 5000, "imu_source": "orientation",
}


def _spin(imu, odom=None, reached=True, aborted=False, slip_pct=29.0):
    """构造一帧 spin_probe 结果。

    不传 `odom` 时按**理论打滑率**反算 —— 四驱同速的阿克曼车转弯必然
    打滑（δ=0.35 时约 29%），odom 一定大于 IMU。
    以前夹具里 odom≈imu 是物理上不可能的值，会让打滑判据测不到真实行为。

    传 `odom` 时按给定值（用于构造异常场景，如打滑远超理论）。
    """
    data = dict(SPIN_OK)
    data["imu_yaw_deg"] = imu
    if odom is None:
        sign = 1.0 if imu >= 0 else -1.0
        odom = imu * (1.0 + slip_pct / 100.0)
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
                # 轮距与轴距决定理论打滑率；不给的话打滑判据不生效
                "track_m": 0.1631, "slip_tolerance_pct": 15.0,
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
            ctx, self.BASELINE["motion"]["spin"], result,
            self.BASELINE["motion"]["safety"], wheelbase=0.261,
            track=self.BASELINE["motion"]["spin"]["track_m"])
        return statuses, verified, result, recorder

    def test_small_diff_passes_and_verifies(self, monkeypatch):
        statuses, verified, result, _ = self._run(
            monkeypatch, [_spin(360.0), _spin(-360.0)])
        assert all(s is Status.PASS for s in statuses)
        assert verified is True

    def test_medium_posture_error_warns(self, monkeypatch):
        """IMU 姿态误差 20° 在 15~30 之间：告警，不算验证通过。

        判据是「IMU 相对目标」，不是「IMU 与 odom 之差」——
        后者被必然打滑污染，常年 100°+。
        """
        statuses, verified, _, _ = self._run(
            monkeypatch, [_spin(380.0), _spin(-380.0)])
        assert any(s is Status.WARN for s in statuses)
        assert verified is False

    def test_large_posture_error_fails(self, monkeypatch):
        """姿态误差 60° 远超容差：失败。"""
        statuses, verified, _, _ = self._run(
            monkeypatch, [_spin(420.0), _spin(-420.0)])
        assert any(s is Status.FAIL for s in statuses)
        assert verified is False

    def test_one_side_bad_blocks_verification(self, monkeypatch):
        """左转准、右转差 —— 不能因为一侧通过就放行。"""
        statuses, verified, _, _ = self._run(
            monkeypatch, [_spin(360.0), _spin(-420.0)])
        assert verified is False

    def test_expected_slip_passes(self, monkeypatch):
        """29% 打滑是四驱同速的必然结果，不能因此判失败。

        2026-11-10 实测：IMU 360.68°、odom 463.28°，打滑 28.4%。
        旧判据拿 102.6° 的差值判 FAIL，等于用物理上达不到的标准判它。
        """
        statuses, verified, _, _ = self._run(
            monkeypatch, [_spin(360.5), _spin(-360.5)])
        assert all(s is Status.PASS for s in statuses), statuses
        assert verified is True

    def test_abnormal_slip_is_flagged(self, monkeypatch):
        """打滑远超理论值说明有异常（轮胎磨损、地面油污、某轮卡死）。"""
        # imu 准，但 odom 是 3 倍 —— 打滑 200%，远超理论 29%
        statuses, verified, _, _ = self._run(
            monkeypatch, [_spin(360.5, 1080.0), _spin(-360.5, -1080.0)])
        assert any(s is Status.FAIL for s in statuses), statuses
        assert verified is False

    def test_unreached_spin_does_not_verify(self, monkeypatch):
        # 没转到 360°（异常提前停）不能算验证过
        statuses, verified, _, _ = self._run(
            monkeypatch, [_spin(120.0, reached=False),
                          _spin(-360.0)])
        assert verified is False

    def test_aborted_spin_does_not_verify(self, monkeypatch):
        statuses, verified, _, _ = self._run(
            monkeypatch, [_spin(100.0, reached=False, aborted=True),
                          _spin(-360.0)])
        assert verified is False

    def test_probe_failure_fails(self, monkeypatch):
        statuses, verified, _, _ = self._run(monkeypatch, [None, _spin(-360)])
        assert any(s is Status.FAIL for s in statuses)
        assert verified is False

    def test_left_and_right_both_probed(self, monkeypatch):
        _, _, _, recorder = self._run(
            monkeypatch, [_spin(360.0), _spin(-360.0)])
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
        recorder = _ProbeRecorder([_spin(360), _spin(-360)])
        monkeypatch.setattr(motion, "run_probe", recorder)
        ctx = _FakeCtx(baseline, "full")
        result = CheckResult(number=18, title="运动控制", status=Status.PASS)
        motion._run_spin(ctx, baseline["motion"]["spin"], result,
                         baseline["motion"]["safety"], wheelbase=0.261,
                         track=baseline["motion"]["spin"].get("track_m", 0))
        assert float(recorder.calls[0][1][0]) <= 0.25


class TestGroundRunsSpin:
    """落地段必须真的执行转圈互校。

    回归：_run_ground 曾把 ground 段当成整个 motion 传进 _run_spin，
    而 _run_spin 读的是 cfg["spin"]，于是永远走「配置缺失，未执行」分支。
    实车报告里表现为：落地段有前进/后退/左右转，却一行转圈数据也没有，
    第 18 项被拖成 SKIP，报告还会写「存在失败项」。
    """

    BASELINE = {
        "motion": {
            "ground": {
                "forward_speed": 0.1, "forward_duration": 0.1,
                "forward_expected_m": 0.3, "forward_tolerance": 0.2,
                "reverse_speed": -0.1, "reverse_duration": 0.1,
                "reverse_expected_m": 0.3, "reverse_tolerance": 0.2,
                "steer_angle": 0.3, "steer_duration": 0.1,
                "steer_expected_yaw_deg": 11, "steer_tolerance_deg": 6,
            },
            "spin": {
                "speed": 0.12, "steer_angle": 0.35, "target_yaw_deg": 360,
                "max_seconds": 90, "yaw_tolerance_deg": 15,
                "yaw_warn_deg": 30, "closure_warn_m": 0.3,
            },
            "safety": {"spin_max_speed_mps": 0.25, "spin_max_seconds": 120},
        }
    }

    def _motion(self):
        return {"ok": True, "dist_m": 0.30, "dyaw_deg": 11.0,
                "avg_speed": 0.10, "duration": 3.0}

    def test_ground_segment_calls_spin_probe(self, monkeypatch):
        from checks import motion

        recorder = _ProbeRecorder([
            self._motion(), self._motion(),   # 落地·前进 / 落地·后退
            self._motion(), self._motion(),   # 落地·左转 / 落地·右转
            _spin(360.0), _spin(-360.0),
        ])
        monkeypatch.setattr(motion, "run_probe", recorder)
        ctx = _FakeCtx(self.BASELINE, "full")
        result = CheckResult(number=18, title="运动控制", status=Status.PASS)

        statuses, verified = motion._run_ground(
            ctx, self.BASELINE["motion"]["ground"], result, 0.5, 10.0,
            self.BASELINE["motion"]["safety"],
            spin_cfg=self.BASELINE["motion"]["spin"])

        names = [c[0] for c in recorder.calls]
        assert names.count("spin_probe") == 2, (
            f"落地段的探针调用是 {names}，转圈没执行"
        )
        assert verified is True

    def test_missing_spin_config_still_skips_not_crashes(self, monkeypatch):
        """老版本 baseline 没有 spin 段：退化为 SKIP，不能报错。"""
        from checks import motion

        recorder = _ProbeRecorder([
            self._motion(), self._motion(), self._motion(), self._motion(),
        ])
        monkeypatch.setattr(motion, "run_probe", recorder)
        ctx = _FakeCtx(self.BASELINE, "full")
        result = CheckResult(number=18, title="运动控制", status=Status.PASS)

        statuses, verified = motion._run_ground(
            ctx, self.BASELINE["motion"]["ground"], result, 0.5, 10.0,
            self.BASELINE["motion"]["safety"], spin_cfg=None)

        assert verified is False
        assert Status.SKIP in statuses
        assert [c[0] for c in recorder.calls].count("spin_probe") == 0


class TestSkipSummaryIsNotFailure:
    """SKIP 的摘要不能写成「存在失败项」。

    回归：2026-11-07 20:06 的实车报告里，落地段四个子项全部 PASS，
    只因转圈缺项而被 judge_all 合并成 SKIP（部分 PASS + 部分 SKIP → SKIP），
    摘要却写「运动测试存在失败项」—— 用户会去找一个根本不存在的故障。
    """

    BASELINE = {
        "motion": {
            "micro_pulse": {"speed": 0.05, "duration": 0.1,
                            "min_displacement_m": 0.0005},
            "bench": {
                "forward_speed": 0.1, "forward_duration": 0.1,
                "min_displacement_m": 0.005,
                "reverse_speed": -0.1, "reverse_duration": 0.1,
                "steer_angle": 0.3, "steer_duration": 0.1,
                "steer_speed": 0.05,
            },
            "ground": {
                "forward_speed": 0.1, "forward_duration": 0.1,
                "forward_expected_m": 0.3, "forward_tolerance": 0.2,
                "reverse_speed": -0.1, "reverse_duration": 0.1,
                "reverse_expected_m": 0.3, "reverse_tolerance": 0.2,
                "steer_angle": 0.3, "steer_duration": 0.1,
                "steer_expected_yaw_deg": 11, "steer_tolerance_deg": 6,
            },
            # 故意不放 spin：这是导致 SKIP 的缺项
            "safety": {"spin_max_speed_mps": 0.25, "spin_max_seconds": 120},
        }
    }

    def test_skip_summary_does_not_claim_failure(self, monkeypatch):
        from checks import motion

        def m(dyaw=0.0):
            return {"ok": True, "dist_m": 0.30, "dyaw_deg": dyaw,
                    "avg_speed": 0.10, "duration": 3.0}

        # 微脉冲 → 架空前进/后退/左转/右转 → 落地前进/后退/左转/右转
        # 左转 yaw 为正、右转为负，否则 bench 会判「两侧同向 = 一侧反了」
        recorder = _ProbeRecorder([
            m(0.015), m(), m(), m(11.0), m(-11.0),
            m(), m(), m(11.0), m(-11.0),
        ])
        monkeypatch.setattr(motion, "run_probe", recorder)
        ctx = _FakeCtx(self.BASELINE, "full")

        result = motion.run(ctx)

        assert result.status is Status.SKIP, (
            f"期望 SKIP，实际 {result.status}；摘要＝{result.summary}"
        )
        assert "失败" not in result.summary, (
            f"SKIP 却写成失败：{result.summary}"
        )
        assert "未验证" in result.summary, (
            f"摘要没说清是没验完：{result.summary}"
        )


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



class TestSpinUnitConsistency:
    """转圈判定与上报必须用同一个单位：度。

    这是实车暴露的真 bug（2026-11-07 23:35 的报告）：
    `travelled = abs(imu_tracker.total - imu0)` 拿到的是**弧度**，
    而 `stop_at` 来自命令行 `target_yaw_deg`，是**度**。

    转满一圈只累积 6.28（弧度），阈值写着 360（度），
    `6.28 >= 360` 永假 —— 探针永远不判 reached，车一直转到 90 秒超时。

    实车表现（raw.log 原文）：
        spin_left  超时 90s 未达到 360°, imu_yaw_deg=688.4, odom=875.38
        spin_right 超时 90s 未达到 360°, imu_yaw_deg=-614.61, odom=-857.66
    车转了两圈多才停，闭合误差 1.39 m。

    原有 TestYawAccumulation 只测了 `YawTracker.degrees`，
    **没测循环里的比较**，所以这个 bug 溜过去了。
    """

    @staticmethod
    def _load():
        import math
        import sys
        import types

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
        return ns["covered_deg"], math

    def test_one_full_turn_is_360_degrees_not_6_28(self):
        """转满一圈必须给出 360，不是 2π。

        这一条直接复现原 bug：修复前返回 6.28，与阈值 360 比较永假。
        """
        covered_deg, math = self._load()
        value = covered_deg(2 * math.pi, 0.0)
        assert abs(value - 360.0) < 0.01, (
            f"转一圈得到 {value}，单位不是度（应当 360）"
        )

    def test_360_reaches_a_360_target(self):
        """转满 360° 时，`travelled >= stop_at` 必须成立。

        这是 reached 的判定条件本身，不是推导。
        """
        covered_deg, math = self._load()
        stop_at = 360.0
        assert covered_deg(2 * math.pi, 0.0) >= stop_at

    def test_real_vehicle_reading_matches_reported_degrees(self):
        """用实车 raw.log 的弧度值复算，应得到报告里那个度数。

        spin_left 的 imu_yaw_deg 报的是 688.4°，对应弧度差约 12.01。
        """
        covered_deg, math = self._load()
        rad = math.radians(688.4)
        assert abs(covered_deg(rad, 0.0) - 688.4) < 0.1

    def test_negative_direction_uses_absolute(self):
        """右转（负方向）也按绝对值比较，与 stop_at 的正数语义一致。"""
        covered_deg, math = self._load()
        assert abs(covered_deg(-2 * math.pi, 0.0) - 360.0) < 0.01

    def test_anomaly_threshold_is_reachable(self):
        """1.3 倍阈值（468°）必须能被达到，否则异常检测永不触发。

        原 bug 下 anomaly_at=468 也永不成立，所以只能超时 ——
        这正是报告里「超时 90s」而非「已转过 xxx° 判定异常」的原因。
        """
        covered_deg, math = self._load()
        anomaly_at = 360.0 * 1.3
        assert covered_deg(math.radians(470), 0.0) >= anomaly_at

    def test_base_offset_is_subtracted(self):
        """起点不是 0 时也要正确：只算相对变化量。"""
        covered_deg, math = self._load()
        base = math.radians(-170)
        now = base + 2 * math.pi
        assert abs(covered_deg(now, base) - 360.0) < 0.01

    def test_probe_uses_the_shared_function_in_both_places(self):
        """判定与上报走同一个转换函数。

        单位一旦在两个地方各写一遍，就会再次漂移。
        这里断言源码里不再出现裸的 `abs(imu_tracker.total - imu0)`
        直接拿去比较的写法。
        """
        from core.paths import resource_path
        src = resource_path("probes/spin_probe.py").read_text(encoding="utf-8")
        assert "travelled = covered_deg(" in src, (
            "循环判定没有用 covered_deg()，单位可能又散了"
        )
        assert "travelled = abs(imu_tracker.total - imu0)" not in src, (
            "出现了裸的弧度比较，会重现永不 reached 的 bug"
        )


class TestSpinGeometryWiring:
    """轴距/轮距必须真的传到 _run_spin，否则打滑判据会静默失效。

    2026-11-10 实车报告里写着「缺轴距/轮距参数，无法与理论值对比」——
    因为 `track` 写成了从 spin 段读，而 baseline 里它在 motion.track_m
    （与 spin 平级），永远读到 0。
    这种「配置写了但读不到」的 bug 不会报错，只会让判据静默降级。
    """

    def test_run_passes_track_from_motion_top_level(self):
        """run() 要从 motion 顶层取 track_m 并传下去。"""
        import inspect
        from checks import motion
        src = inspect.getsource(motion.run)
        assert '"track_m"' in src and "cfg.get(" in src, (
            "run() 没从 motion 顶层读 track_m，打滑判据会拿不到轮距"
        )
        assert "track=" in src, "run() 没把 track 传给 _run_ground"

    def test_run_ground_forwards_track_to_spin(self):
        import inspect
        from checks import motion
        src = inspect.getsource(motion._run_ground)
        assert "track=track" in src, "_run_ground 没把 track 转给 _run_spin"

    def test_geometry_measured_when_track_given(self, monkeypatch):
        """给了轮距就该出现「转向几何」测量项。"""
        from checks import motion
        from core.verdict import CheckResult, Status
        baseline = {"motion": {"spin": {
            "speed": 0.12, "steer_angle": 0.35, "target_yaw_deg": 360,
            "max_seconds": 90, "yaw_tolerance_deg": 15, "yaw_warn_deg": 30,
            "closure_warn_m": 0.3, "track_m": 0.1631, "slip_tolerance_pct": 15,
        }, "safety": {"spin_max_speed_mps": 0.25, "spin_max_seconds": 120}}}
        rec = _ProbeRecorder([_spin(360.5), _spin(-360.5)])
        monkeypatch.setattr(motion, "run_probe", rec)
        ctx = _FakeCtx(baseline, "full")
        res = CheckResult(number=18, title="运动控制", status=Status.PASS)
        motion._run_spin(ctx, baseline["motion"]["spin"], res,
                         baseline["motion"]["safety"],
                         wheelbase=0.261, track=0.1631)
        names = [m.name for m in res.measurements]
        assert "转向几何" in names, f"没有几何项：{names}"
        geom = next(m for m in res.measurements if m.name == "转向几何")
        assert "理论打滑" in str(geom.value)

    def test_no_geometry_without_track(self, monkeypatch):
        """不给轮距就不该凭空算打滑 —— 缺参数要显式缺席，不能猜。"""
        from checks import motion
        from core.verdict import CheckResult, Status
        baseline = {"motion": {"spin": {
            "speed": 0.12, "steer_angle": 0.35, "target_yaw_deg": 360,
            "max_seconds": 90, "yaw_tolerance_deg": 15, "yaw_warn_deg": 30,
            "closure_warn_m": 0.3,
        }, "safety": {"spin_max_speed_mps": 0.25, "spin_max_seconds": 120}}}
        rec = _ProbeRecorder([_spin(360.5), _spin(-360.5)])
        monkeypatch.setattr(motion, "run_probe", rec)
        ctx = _FakeCtx(baseline, "full")
        res = CheckResult(number=18, title="运动控制", status=Status.PASS)
        motion._run_spin(ctx, baseline["motion"]["spin"], res,
                         baseline["motion"]["safety"],
                         wheelbase=0.261, track=0)
        names = [m.name for m in res.measurements]
        assert "转向几何" not in names

    def test_baseline_declares_both_geometry_values(self):
        """产品配置必须同时给出轴距与轮距，否则几何判据不完整。"""
        import yaml
        from core.paths import resource_path
        data = yaml.safe_load(
            resource_path("products/tianracer_t110_rdk/baseline.yaml")
            .read_text(encoding="utf-8"))
        motion_cfg = data["motion"]
        assert motion_cfg.get("wheelbase_m"), "baseline 缺 wheelbase_m"
        assert motion_cfg.get("track_m"), "baseline 缺 track_m"
        assert motion_cfg["spin"].get("slip_tolerance_pct"), \
            "baseline 缺 slip_tolerance_pct"
