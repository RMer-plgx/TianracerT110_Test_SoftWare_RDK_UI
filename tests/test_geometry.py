"""阿克曼转向几何的计算正确性。

这些公式是整个标定结论的地基：如果 `R = L/tan(δ)` 或
`δ = atan(L/R)` 写错，拼出来的「真实转角」就是错的，
而报告会照样显示精致的小数位。
"""

from __future__ import annotations

import math

import pytest

from core.geometry import (closure_after_turn, correct_for_slip,
                           slip_ratio, spin_seconds, steer_from_measurement,
                           steer_from_radius, summarize, turning_radius,
                           yaw_rate_rad)

L = 0.255   # 2026-11-10 实测轴距


class TestTurningRadius:
    def test_known_value(self):
        """δ=0.35 rad 时半径应约 0.70 m（26 英寸左右的轴距，常见比例）。"""
        assert turning_radius(L, 0.35) == pytest.approx(0.699, abs=0.005)

    def test_radius_grows_as_steer_shrinks(self):
        """转角越小，半径越大 —— 单调性，防止公式写反。"""
        radii = [turning_radius(L, d) for d in (0.35, 0.30, 0.25, 0.20, 0.15, 0.10)]
        assert radii == sorted(radii)

    def test_straight_line_has_no_radius(self):
        """转角 0 是直线，半径无穷大，返回 None 而不是除零。"""
        assert turning_radius(L, 0.0) is None
        assert turning_radius(L, 1e-6) is None

    def test_sign_does_not_matter(self):
        """左右转半径相同，方向由别的字段表达。"""
        assert turning_radius(L, 0.25) == pytest.approx(turning_radius(L, -0.25))

    def test_zero_wheelbase_is_rejected(self):
        assert turning_radius(0.0, 0.3) is None


class TestRoundTrip:
    """δ → R → δ 必须回到原点。"""

    @pytest.mark.parametrize("steer", [0.10, 0.15, 0.20, 0.25, 0.30, 0.35])
    def test_steer_to_radius_to_steer(self, steer):
        radius = turning_radius(L, steer)
        assert steer_from_radius(L, radius) == pytest.approx(steer, rel=1e-9)

    @pytest.mark.parametrize("steer", [0.10, 0.25, 0.35])
    def test_yaw_rate_matches_geometry(self, steer):
        """ω = v/R，且从 ω 反推回同一个 δ。"""
        v = 0.12
        omega = yaw_rate_rad(L, steer, v)
        assert omega == pytest.approx(v / turning_radius(L, steer))
        back = steer_from_measurement(L, v, math.degrees(omega))
        assert back == pytest.approx(steer, rel=1e-6)


class TestSpinSeconds:
    def test_matches_manual_calculation(self):
        """转一圈时间 = 2π / ω。"""
        v, steer = 0.12, 0.35
        omega = yaw_rate_rad(L, steer, v)
        assert spin_seconds(L, steer, v, 360) == pytest.approx(2 * math.pi / omega)

    def test_smaller_steer_takes_longer(self):
        fast = spin_seconds(L, 0.35, 0.12, 360)
        slow = spin_seconds(L, 0.10, 0.12, 360)
        assert slow > fast * 3      # 0.10 的半径是 0.35 的 3.6 倍

    def test_straight_line_never_finishes(self):
        assert spin_seconds(L, 0.0, 0.12, 360) is None


class TestSlipRatio:
    def test_odom_ahead_means_slip(self):
        """轮子比车多转 = 打滑，返回正数。"""
        assert slip_ratio(875.38, 688.4) == pytest.approx(0.2716, abs=1e-3)

    def test_no_difference_no_slip(self):
        assert slip_ratio(360.0, 360.0) == pytest.approx(0.0)

    def test_negative_slip_is_reported_not_clamped(self):
        """odom 落后于 IMU 也应如实返回，让调用方判断。"""
        assert slip_ratio(300.0, 360.0) < 0

    def test_zero_imu_is_undefined(self):
        assert slip_ratio(100.0, 0.0) is None
        assert slip_ratio(100.0, None) is None
        assert slip_ratio(None, 100.0) is None


class TestSlipCorrection:
    def test_recovers_true_speed(self):
        """27% 打滑时，指令 0.12 对应的真实速度约 0.094。"""
        assert correct_for_slip(0.12, 0.2716) == pytest.approx(0.0944, abs=1e-3)

    def test_zero_slip_is_passthrough(self):
        assert correct_for_slip(0.12, 0.0) == pytest.approx(0.12)

    def test_none_slip_is_passthrough(self):
        assert correct_for_slip(0.12, None) == pytest.approx(0.12)

    def test_extreme_slip_does_not_explode(self):
        """s 接近 -1 时不能除出无穷大。"""
        assert math.isfinite(correct_for_slip(0.12, -0.999))


class TestRealVehicleCase:
    """用 2026-11-07 实车 raw.log 的原始值验算。

    spin_left: imu=688.4°, odom=875.38°, duration=91.32s, speed=0.12, steer=0.35
    这一轮车转了 1.91 圈、闭合误差 1.39 m，是单位 bug 导致的超时。
    """

    IMU = 688.4
    ODOM = 875.38
    DURATION = 91.32
    SPEED = 0.12
    STEER_CMD = 0.35

    def test_uncorrected_steer_underestimates(self):
        """不修正打滑会明显低估转角 —— 这是必须在报告里标出来的偏差。"""
        omega = self.IMU / self.DURATION
        steer = steer_from_measurement(L, self.SPEED, omega)
        assert math.degrees(steer) == pytest.approx(15.6, abs=0.3)
        assert math.degrees(steer) < math.degrees(self.STEER_CMD) - 3

    def test_slip_corrected_steer_is_close_to_command(self):
        """修正打滑后应贴近指令值（20.05°），残差 < 1.5°。

        这条同时验证「打滑修正」这个思路本身是有效的。
        """
        slip = slip_ratio(self.ODOM, self.IMU)
        v_real = correct_for_slip(self.SPEED, slip)
        omega = self.IMU / self.DURATION
        steer = steer_from_measurement(L, v_real, omega)
        assert math.degrees(steer) == pytest.approx(20.05, abs=1.5)

    def test_theoretical_vs_measured_omega_shows_slip(self):
        """理论 9.84°/s 与实际 7.54°/s 的差，正是打滑率的量级。"""
        theoretical = math.degrees(yaw_rate_rad(L, self.STEER_CMD, self.SPEED))
        measured = self.IMU / self.DURATION
        assert theoretical == pytest.approx(9.84, abs=0.2)
        assert measured == pytest.approx(7.54, abs=0.2)
        assert (theoretical - measured) / theoretical == pytest.approx(0.23, abs=0.05)


class TestClosure:
    def test_full_turn_returns_to_start(self):
        """转满一圈闭合误差为 0。"""
        assert closure_after_turn(0.7, 360.0) == pytest.approx(0.0, abs=1e-9)

    def test_half_turn_is_diameter(self):
        """转半圈时终点在直径另一端。"""
        assert closure_after_turn(0.7, 180.0) == pytest.approx(1.4, abs=1e-9)

    def test_quarter_turn_is_sqrt2_times_radius(self):
        assert closure_after_turn(1.0, 90.0) == pytest.approx(math.sqrt(2), abs=1e-9)


class TestSummarize:
    def test_shape_and_values(self):
        s = summarize(L, 0.35, 0.12, 360)
        assert s["wheelbase_m"] == L
        assert s["steer_cmd_deg"] == pytest.approx(20.05, abs=0.01)
        assert s["radius_m"] == pytest.approx(0.699, abs=0.005)
        assert s["seconds_for_target"] == pytest.approx(36.6, abs=0.5)
        assert s["curve_length_m"] == pytest.approx(4.39, abs=0.05)

    def test_straight_line_yields_nones(self):
        s = summarize(L, 0.0, 0.12, 360)
        assert s["radius_m"] is None
        assert s["yaw_rate_deg_s"] is None
        assert s["seconds_for_target"] is None


class TestSlipSymmetry:
    """左右转的打滑率必须同号同值。

    这是 2026-11-10 标定暴露的真 bug：右转时 odom 与 IMU 都是负数，
    用带符号差值 `(odom - imu)` 会算出负打滑，而负打滑会把修正车速
    变大、反推成假的小转角。

    实车后果：左转 δ_B=19.0°、右转 δ_B=8.8°，看起来像右转舵机坏了一半。
    """

    # raw.log: calib_left_0.35 / calib_right_0.35，10 秒
    LEFT_IMU, LEFT_ODOM = 72.8, 94.0
    RIGHT_IMU, RIGHT_ODOM = -67.5, -92.9

    def test_both_directions_give_positive_slip(self):
        left = slip_ratio(self.LEFT_ODOM, self.LEFT_IMU)
        right = slip_ratio(self.RIGHT_ODOM, self.RIGHT_IMU)
        assert left > 0, f"左转打滑应为正，得到 {left}"
        assert right > 0, f"右转打滑应为正，得到 {right}"

    def test_magnitudes_are_close(self):
        """同一转角、同一路面，左右打滑率应接近。"""
        left = slip_ratio(self.LEFT_ODOM, self.LEFT_IMU)
        right = slip_ratio(self.RIGHT_ODOM, self.RIGHT_IMU)
        assert abs(left - right) < 0.15, f"左右打滑差异过大：{left:.3f} vs {right:.3f}"

    def test_corrected_steer_matches_both_sides(self):
        """修正后的反推转角，左右应在 1° 内一致。"""
        L = 0.255
        out = {}
        for name, imu, odom in (("left", self.LEFT_IMU, self.LEFT_ODOM),
                                ("right", self.RIGHT_IMU, self.RIGHT_ODOM)):
            slip = slip_ratio(odom, imu)
            v_real = correct_for_slip(0.12, slip)
            omega = abs(imu) / 10.0
            out[name] = math.degrees(steer_from_measurement(L, v_real, omega))
        assert abs(out["left"] - out["right"]) < 1.0, (
            f"反推转角左右不一致：{out}"
        )

    def test_sign_flip_does_not_change_magnitude(self):
        """把 odom/imu 同时取反（换转向方向），打滑率不变。"""
        a = slip_ratio(94.0, 72.8)
        b = slip_ratio(-94.0, -72.8)
        assert a == pytest.approx(b)
