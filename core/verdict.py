"""检测结果的数据结构与判定逻辑。

这里只放纯函数和纯数据，不碰网络、不碰文件系统，
这样 tests/test_verdict.py 可以直接覆盖最容易出错的判定规则。

设计原则：宁可误报，不可漏报。拿不到数据不等于合格。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class Status(enum.Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"
    MANUAL = "manual"

    @property
    def marker(self) -> str:
        return {
            Status.PASS: "✅",
            Status.WARN: "⚠️",
            Status.FAIL: "❌",
            Status.SKIP: "⏭️",
            Status.MANUAL: "👤",
        }[self]

    @property
    def counts_as_pass(self) -> bool:
        """SKIP 不算通过。这是防漏报的核心规则。

        拿不到数据就是没验证过，不能因为「没有坏消息」就当成好消息。
        """
        return self is Status.PASS or self is Status.MANUAL


@dataclass
class Measurement:
    """单条测量记录。name/value 会原样进报告表格。"""

    name: str
    value: Any
    unit: str = ""
    expected: Optional[str] = None
    note: str = ""

    def display(self) -> str:
        text = f"{self.value}"
        if self.unit:
            text = f"{text} {self.unit}"
        return text


@dataclass
class CheckResult:
    """一项检测的结果。"""

    number: int
    title: str
    status: Status
    summary: str = ""
    measurements: List[Measurement] = field(default_factory=list)
    commands: List[str] = field(default_factory=list)
    detail: str = ""
    fatal: bool = False
    duration_sec: float = 0.0
    # 该检测属于哪个测试阶段：bench（架空）/ ground（落地）/ static（无需移动）
    phase: str = "static"

    def add(self, name: str, value: Any, unit: str = "",
            expected: Optional[str] = None, note: str = "") -> None:
        self.measurements.append(
            Measurement(name=name, value=value, unit=unit,
                        expected=expected, note=note)
        )

    def add_command(self, cmd: str) -> None:
        self.commands.append(cmd)


# 未验证比例的容忍上限：SKIP 项占可判定项超过这个比例就不算交付合格
MAX_SKIP_RATIO = 0.2


@dataclass
class Report:
    """整次测试的汇总容器。"""

    host: str
    user: str
    started_at: str
    finished_at: str = ""
    results: List[CheckResult] = field(default_factory=list)
    answers: Dict[str, str] = field(default_factory=dict)
    environment: Dict[str, str] = field(default_factory=dict)
    aborted_reason: str = ""
    voltage_v: Optional[float] = None

    def add(self, result: CheckResult) -> None:
        self.results.append(result)

    @property
    def failed(self) -> List[CheckResult]:
        return [r for r in self.results if r.status is Status.FAIL]

    @property
    def warned(self) -> List[CheckResult]:
        return [r for r in self.results if r.status is Status.WARN]

    @property
    def passed(self) -> List[CheckResult]:
        return [r for r in self.results if r.status is Status.PASS]

    @property
    def skipped(self) -> List[CheckResult]:
        return [r for r in self.results if r.status is Status.SKIP]

    @property
    def executed(self) -> List[CheckResult]:
        """真正跑了的项（不含 SKIP 和 MANUAL）。"""
        return [r for r in self.results
                if r.status in (Status.PASS, Status.WARN, Status.FAIL)]

    @property
    def judged(self) -> List[CheckResult]:
        """可参与判定的项：排除人工项，但 SKIP 计入。"""
        return [r for r in self.results if r.status is not Status.MANUAL]

    @property
    def pass_ratio(self) -> float:
        """已执行项里的通过比例。**仅供展示，不用于判定**。"""
        executed = self.executed
        if not executed:
            return 0.0
        return len(self.passed) / len(executed)

    @property
    def skip_ratio(self) -> float:
        """未验证项占比。判定看这个，而不是 pass_ratio。

        原因：如果用 executed 作分母，则 9 项 SKIP + 1 项 PASS 会算出
        100% 通过——这正是典型的漏报。
        """
        judged = self.judged
        if not judged:
            return 1.0
        return len(self.skipped) / len(judged)

    @property
    def verdict(self) -> str:
        """交付判定。

        规则（防漏报）：
        1. 中途终止 → 不通过
        2. 致命项 FAIL → 直接不通过
        3. 任何 FAIL → 不通过
        4. SKIP 过多或通过率不足 → 不通过（数据缺失不等于合格）
        5. 有 SKIP/WARN → 可交付但需确认
        6. 全 PASS → 具备交付条件
        """
        if not self.results:
            return "未产生任何检测结果，无法判定"

        if self.aborted_reason:
            return f"测试中途终止：{self.aborted_reason}"

        if any(r.status is Status.FAIL and r.fatal for r in self.results):
            return "不具备交付条件（存在致命项失败）"

        if self.failed:
            return "不具备交付条件（存在失败项）"

        judged = self.judged
        if not judged:
            return "不具备交付条件（没有可判定的检测项）"

        if not self.executed:
            return "不具备交付条件（所有检测项均未取得数据）"

        # SKIP 意味着「没验证」，超过容忍比例就不能说合格
        if self.skip_ratio > MAX_SKIP_RATIO:
            return (f"不具备交付条件（{len(self.skipped)}/{len(judged)} 项未取得数据，"
                    f"未验证比例 {self.skip_ratio:.0%} 超过 {MAX_SKIP_RATIO:.0%}）")

        if self.skipped:
            titles = "、".join(r.title for r in self.skipped[:3])
            more = "等" if len(self.skipped) > 3 else ""
            return f"可交付，但需确认 {len(self.skipped)} 项未验证（{titles}{more}）"

        if self.warned:
            return "可交付，但存在需要确认的告警项"

        return "具备交付条件"

    @property
    def verdict_class(self) -> str:
        """给 HTML 模板用的 CSS class。"""
        if self.failed or self.skip_ratio > MAX_SKIP_RATIO:
            return "bad"
        if self.warned or self.skipped:
            return "warn"
        return "good"


# ---------------------------------------------------------------------------
# 判定函数（纯函数，单元测试重点覆盖）
# ---------------------------------------------------------------------------

def judge_range(value: Optional[float], low: float, high: float,
                warn_margin: float = 0.0) -> Status:
    """按区间判定。

    value 为 None 视为 SKIP（表示没取到数据，不是合格）。
    warn_margin > 0 时，落在 [low, low+warn_margin) 或 (high-warn_margin, high]
    区间内判 WARN，用于「勉强合格」的提示。
    """
    if value is None:
        return Status.SKIP
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return Status.SKIP
    if numeric < low or numeric > high:
        return Status.FAIL
    margin = max(0.0, warn_margin)
    if margin > 0 and (numeric < low + margin or numeric > high - margin):
        return Status.WARN
    return Status.PASS


def judge_voltage(volts: Optional[float], normal_v: float, low_v: float) -> Status:
    """电压判定。

    >= normal_v    满电/正常     PASS
    >= low_v       偏低          WARN
    <  low_v       必须充电      FAIL
    """
    if volts is None:
        return Status.SKIP
    try:
        v = float(volts)
    except (TypeError, ValueError):
        return Status.SKIP
    if v >= normal_v:
        return Status.PASS
    if v >= low_v:
        return Status.WARN
    return Status.FAIL


def judge_envvar(actual: Optional[str], expected: str,
                 known_deviation: Optional[dict] = None) -> Status:
    """环境变量判定。

    已知偏差（config 中 envvars_known_deviations 声明过的）判 WARN，
    其他不符判 FAIL，完全一致判 PASS。
    """
    if actual is None:
        return Status.FAIL
    if actual == expected:
        return Status.PASS
    if known_deviation and known_deviation.get("actual") == actual:
        return Status.WARN
    return Status.FAIL


def judge_displacement(displacement_m: Optional[float], minimum_m: float) -> Status:
    """微脉冲判定：只要有可辨认的位移就算底盘响应了。"""
    if displacement_m is None:
        return Status.SKIP
    try:
        d = abs(float(displacement_m))
    except (TypeError, ValueError):
        return Status.SKIP
    return Status.PASS if d >= minimum_m else Status.FAIL


def judge_distance(actual_m: Optional[float], expected_m: float,
                   tolerance: float) -> Status:
    """移动距离判定（相对误差）。

    expected_m 为 0 或负数时无法计算相对误差，返回 SKIP 而不是静默通过。
    """
    if actual_m is None:
        return Status.SKIP
    if expected_m <= 0:
        return Status.SKIP
    try:
        actual = abs(float(actual_m))
    except (TypeError, ValueError):
        return Status.SKIP
    error = abs(actual - expected_m) / expected_m
    return Status.PASS if error <= tolerance else Status.FAIL


def judge_repeats(values: List[Optional[float]], low: float, high: float,
                  warn_margin: float = 0.0) -> Status:
    """多次测量取最差值判定。

    用于「左右转向各测一次」这类场景：任一次不合格即整体不合格，
    避免单向测了合格就放行。
    """
    if not values:
        return Status.SKIP
    statuses = [judge_range(v, low, high, warn_margin) for v in values]
    if any(s is Status.FAIL for s in statuses):
        return Status.FAIL
    if any(s is Status.WARN for s in statuses):
        return Status.WARN
    if all(s is Status.SKIP for s in statuses):
        return Status.SKIP
    return Status.PASS


def judge_all(statuses: List[Status], fail_is_warn: bool = False) -> Status:
    """把多个子项的判定合成一个总判定。

    fail_is_warn=True 时，子项的 FAIL 降级为 WARN（用于已知偏差场景）。
    """
    if not statuses:
        return Status.SKIP
    if any(s is Status.FAIL for s in statuses):
        return Status.WARN if fail_is_warn else Status.FAIL
    if any(s is Status.WARN for s in statuses):
        return Status.WARN
    if any(s is Status.PASS for s in statuses):
        return Status.PASS
    return Status.SKIP
