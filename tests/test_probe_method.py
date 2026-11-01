"""探针方法学回归测试。

实车踩过的坑：探针用 wait_for_message 循环 + 首末时间戳算频率，
采样帧数少时会把正常设备误判成故障（雷达 30 Hz 被算成 24 Hz）。

这类问题单测发现不了（因为要连真车），但可以断言源码结构：
所有频率类探针必须用 Subscriber 回调计数 + 固定时间窗口。
"""

import re
from pathlib import Path

import pytest

PROBES = Path(__file__).resolve().parent.parent / "probes"


class TestFrequencyMeasurementMethod:
    """频率测量必须用回调计数，不能用 wait_for_message 循环。"""

    @pytest.mark.parametrize("name", ["lidar_probe.py", "odom_probe.py", "imu_probe.py"])
    def test_uses_subscriber_callback(self, name):
        source = (PROBES / name).read_text(encoding="utf-8")
        assert "rospy.Subscriber" in source, (
            f"{name} 没有用 Subscriber 回调计数，频率会测不准"
        )

    @pytest.mark.parametrize("name", ["lidar_probe.py", "odom_probe.py", "imu_probe.py"])
    def test_no_wait_for_message_loop_for_counting(self, name):
        """wait_for_message 循环每次重建订阅，开销大且会丢帧。"""
        source = (PROBES / name).read_text(encoding="utf-8")
        # 允许出现一次用于"确认话题活着"，但不能在循环里反复调用
        in_loop = re.findall(
            r"for\s+_\s+in\s+range\([^)]*\):\s*\n\s*(?:try:\s*\n\s*)?\w+\s*\.?\s*append\(?\s*rospy\.wait_for_message",
            source,
        )
        assert not in_loop, f"{name} 仍在循环里用 wait_for_message 计数"

    @pytest.mark.parametrize("name", ["lidar_probe.py", "odom_probe.py", "imu_probe.py"])
    def test_uses_fixed_time_window(self, name):
        """用固定时间窗口统计，而不是首末时间戳差。"""
        source = (PROBES / name).read_text(encoding="utf-8")
        assert "window" in source.lower(), f"{name} 缺少固定时间窗口"
        assert "rospy.sleep" in source, f"{name} 没有等待采样窗口"

    def test_lidar_window_long_enough(self):
        """雷达采样窗口要够长，6 秒在 30 Hz 下约 180 帧。"""
        source = (PROBES / "lidar_probe.py").read_text(encoding="utf-8")
        match = re.search(r"window\s*=\s*float\(sys\.argv\[2\]\)\s*if.*?else\s*([\d.]+)", source)
        assert match, "找不到 lidar_probe 的默认窗口"
        assert float(match.group(1)) >= 5.0


class TestLidarCallSite:
    """检查项传的参数必须是秒数，且足够长。"""

    def test_lidar_passes_seconds_not_frames(self):
        source = (Path(__file__).resolve().parent.parent
                  / "checks" / "lidar.py").read_text(encoding="utf-8")
        match = re.search(r'run_probe\(ctx,\s*"lidar_probe",\s*"/tianracer/scan",\s*"([\d.]+)"', source)
        assert match, "找不到 lidar 的 run_probe 调用"
        seconds = float(match.group(1))
        assert seconds >= 5.0, (
            f"雷达采样窗口只有 {seconds} 秒，太短会把正常抖动放大成频率异常"
        )
