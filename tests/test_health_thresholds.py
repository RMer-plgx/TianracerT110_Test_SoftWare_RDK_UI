"""系统健康的阈值判定测试。

这里守的是一个实际踩过的坑（2026-11-02 实车）：
温度和负载都是**单侧指标**（越低越好），原本用
`judge_range(v, 0, limit, warn_margin=m)` 判定，等于把
「v < m」也算成告警 —— 空载的车（load 1.26 / 阈值 3.0）
被报成「指标接近阈值」，冷凝的车更会被判告警。

判定必须只朝坏的方向预警：正常 → PASS，超过 warn → WARN，
超过 fail → FAIL。低值永远是好事，不告警。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._fixtures import attach_profile, get_profile  # noqa: E402
from checks import health  # noqa: E402
from core.verdict import Status  # noqa: E402


class _Out:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.stderr = ""
        self.ok = True


def _ctx(load: float, temp_c: float, disk_pct: int = 67, mem_avail: int = 6):
    class _Remote:
        def run(self, cmd, **kwargs):
            if "thermal" in cmd:
                # 毫摄氏度格式，与真机 /sys/class/thermal/.../temp 一致
                return _Out(f"x {int(temp_c * 1000)}\n")
            if "loadavg" in cmd:
                return _Out(f"{load} 1.50 1.70 2/300 12345\n")
            if "df -h" in cmd:
                return _Out(f"/dev/root 30G 20G 10G {disk_pct}% /\n")
            if "free -g" in cmd:
                return _Out(f"8 {mem_avail}\n")
            return _Out("")

    class _Ctx:
        profile = get_profile()
        remote = _Remote()
        baseline = {
            "temp_warn_c": 75.0,
            "temp_fail_c": 85.0,
            "disk_warn_pct": 85.0,
            "disk_fail_pct": 95.0,
            "load_warn": 3.0,
        }

    return _Ctx()


class TestLowValuesAreNotWarnings:
    def test_idle_load_is_pass(self):
        # 实车原始症状：load 1.26 被判 WARN
        assert health.run(_ctx(load=1.26, temp_c=40.0)).status.value == "pass"

    def test_very_low_load_is_pass(self):
        assert health.run(_ctx(load=0.05, temp_c=40.0)).status.value == "pass"

    def test_cold_boot_temperature_is_pass(self):
        # 冬天冷启动读到 5°C 不该告警
        assert health.run(_ctx(load=1.0, temp_c=5.0)).status.value == "pass"

    def test_zero_load_is_pass(self):
        assert health.run(_ctx(load=0.0, temp_c=40.0)).status.value == "pass"


class TestHighValuesStillWarn:
    def test_load_above_warn_is_warn(self):
        assert health.run(_ctx(load=3.5, temp_c=40.0)).status.value == "warn"

    def test_load_far_above_is_fail(self):
        assert health.run(_ctx(load=5.0, temp_c=40.0)).status.value == "fail"

    def test_temperature_above_warn_is_warn(self):
        assert health.run(_ctx(load=1.0, temp_c=80.0)).status.value == "warn"

    def test_temperature_above_fail_is_fail(self):
        assert health.run(_ctx(load=1.0, temp_c=90.0)).status.value == "fail"


class TestDiskStillJudged:
    def test_disk_below_warn_is_pass(self):
        assert health.run(_ctx(1.0, 40.0, disk_pct=67)).status.value == "pass"

    def test_disk_above_warn_is_warn(self):
        assert health.run(_ctx(1.0, 40.0, disk_pct=90)).status.value == "warn"

    def test_disk_above_fail_is_fail(self):
        assert health.run(_ctx(1.0, 40.0, disk_pct=97)).status.value == "fail"

class TestJudgeMaxOnly:
    """单侧指标（越低越好）的通用判定。"""

    def test_low_value_passes(self):
        from core.verdict import judge_max_only
        assert judge_max_only(1.26, 3.0, 4.5) is Status.PASS

    def test_above_warn_is_warn(self):
        from core.verdict import judge_max_only
        assert judge_max_only(3.5, 3.0, 4.5) is Status.WARN

    def test_above_fail_is_fail(self):
        from core.verdict import judge_max_only
        assert judge_max_only(5.0, 3.0, 4.5) is Status.FAIL

    def test_boundary_low_is_pass(self):
        from core.verdict import judge_max_only
        assert judge_max_only(0.0, 3.0, 4.5) is Status.PASS

    def test_none_is_skip_not_pass(self):
        from core.verdict import judge_max_only
        assert judge_max_only(None, 3.0, 4.5) is Status.SKIP


class TestJudgeMinOnly:
    """单侧指标（越高越好）的通用判定。"""

    def test_high_value_passes(self):
        from core.verdict import judge_min_only
        assert judge_min_only(6.0, 1.0, 0.5) is Status.PASS

    def test_below_warn_is_warn(self):
        from core.verdict import judge_min_only
        assert judge_min_only(0.8, 1.0, 0.5) is Status.WARN

    def test_below_fail_is_fail(self):
        from core.verdict import judge_min_only
        assert judge_min_only(0.3, 1.0, 0.5) is Status.FAIL

    def test_none_is_skip(self):
        from core.verdict import judge_min_only
        assert judge_min_only(None, 1.0, 0.5) is Status.SKIP
