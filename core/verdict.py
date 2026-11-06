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
    # 中间态：这一项在**本阶段**已经测了，完整结论要等后续阶段。
    #
    # 用例（用户 2026-11-03）：IMU / 里程计在架空段只能验证频率和静态指标，
    # 数据准不准必须落地跑 360° 互校才知道。架空段一测完就标绿，
    # 等于把「测了一半」当成「测完了」。
    #
    # 关键：counts_as_pass = False，且计入未验证比例。
    # 它不是「放宽」，而是「明确还没验证完」。
    PENDING = "pending"

    @property
    def marker(self) -> str:
        return {
            Status.PASS: "✅",
            Status.WARN: "⚠️",
            Status.FAIL: "❌",
            Status.SKIP: "⏭️",
            Status.MANUAL: "👤",
            Status.PENDING: "⏳",
        }[self]

    @property
    def counts_as_pass(self) -> bool:
        """SKIP 不算通过。这是防漏报的核心规则。

        拿不到数据就是没验证过，不能因为「没有坏消息」就当成好消息。
        PENDING 同理：只测了一半，没验证完就不算通过。
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
    # 本阶段测完但结论要等落地段。置位后由 runner 在落地段结束时回填：
    #   落地互校通过 → PASS；没做落地段 → 保持 PENDING
    # 用例：IMU / 里程计在架空段只能验证频率与静态指标。
    needs_ground: bool = False

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
    # 本次用的是哪个产品配置。报告里要写明，否则同一套报告
    # 分不清是哪个产品的结果（多产品化之后必须有）。
    product: str = ""
    product_id: str = ""
    product_revision: str = ""
    # 生成这份报告的工具名（CLI 版 / UI 版不同）。
    # md 与 html 两个模板必须用同一个值，否则同一份报告会出现两个工具名。
    tool_name: str = "tb-verify"
    # 生成这份报告的软件版本。拿到报告能反查是哪一版生成的，
    # 这在多产品、多现场时尤其重要 —— 否则无法判断某个已知缺陷
    # 是否已被这一版修掉。
    tool_version: str = ""
    # 若本次测试删除了车上的持久文件（ROS 日志等），这里写明，
    # 报告开头要醒目展示 —— 读者必须知道拿到的是「被清过的车」。
    cleanup_declaration: str = ""

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
    def pending(self) -> List[CheckResult]:
        """中间态：本阶段测过，但要等后续阶段才能下结论。"""
        return [r for r in self.results if r.status is Status.PENDING]

    @property
    def unverified(self) -> List[CheckResult]:
        """未验证 = 完全没测（SKIP） + 只测了一半（PENDING）。

        两者对「能不能交付」是同一件事：都还没验证完。
        报告里分开列，是为了让读者知道是「没做」还是「做了一半」。
        """
        return [r for r in self.results
                if r.status in (Status.SKIP, Status.PENDING)]

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
        # PENDING 也计入：只测一半不等于验证过
        return len(self.unverified) / len(judged)

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

        # 致命项停在 PENDING（只测了一半）也不能说可交付。
        #
        # 为什么单列这一条：fatal 的含义是「这项不过，后面测了也没意义」。
        # IMU / 里程计的航向精度要等落地转圈互校才有结论，在那之前它们
        # 既不是 PASS 也不是 FAIL —— 若照常放行，「致命项没验证完」就会
        # 被 skip_ratio 这条宽松阈值（20%）掩盖掉。
        # 报告里仍可人工勾选「确认接受」，那是显式的责任承担。
        fatal_pending = [r for r in self.results
                         if r.fatal and r.status is Status.PENDING]
        if fatal_pending:
            titles = "、".join(r.title for r in fatal_pending[:3])
            return f"不具备交付条件（致命项未完成落地互校：{titles}）"

        judged = self.judged
        if not judged:
            return "不具备交付条件（没有可判定的检测项）"

        if not self.executed:
            # 全 PENDING（只有「测了一半」的项）不能说「未取得数据」——
            # 数据其实是有的，只是还没到能下结论的阶段。
            if self.pending:
                titles = "、".join(r.title for r in self.pending[:3])
                return (f"不具备交付条件（{len(self.pending)} 项待落地验证，"
                        f"尚无任何项完成完整验证：{titles}）")
            return "不具备交付条件（所有检测项均未取得数据）"

        # 未验证（完全没测 + 只测一半）超过容忍比例就不能说合格
        if self.skip_ratio > MAX_SKIP_RATIO:
            return (f"不具备交付条件（{len(self.unverified)}/{len(judged)} 项未验证，"
                    f"未验证比例 {self.skip_ratio:.0%} 超过 {MAX_SKIP_RATIO:.0%}）")

        if self.unverified:
            parts = []
            if self.pending:
                titles = "、".join(r.title for r in self.pending[:2])
                more = "等" if len(self.pending) > 2 else ""
                parts.append(f"{len(self.pending)} 项待落地验证（{titles}{more}）")
            if self.skipped:
                titles = "、".join(r.title for r in self.skipped[:2])
                more = "等" if len(self.skipped) > 2 else ""
                parts.append(f"{len(self.skipped)} 项未验证（{titles}{more}）")
            return "可交付，但需确认 " + "，".join(parts)

        if self.warned:
            return "可交付，但存在需要确认的告警项"

        return "具备交付条件"

    @property
    def verdict_class(self) -> str:
        """给 HTML 模板用的 CSS class。

        中止的报告不能用绿色，否则读者只看颜色会误以为合格（审查发现）。
        """
        if self.aborted_reason:
            return "bad"
        if self.failed or self.skip_ratio > MAX_SKIP_RATIO:
            return "bad"
        # 与 verdict 保持一致：致命项停在 PENDING 也是「不具备交付条件」，
        # 颜色必须是红的。两处分开实现很容易漂移，所以条件写全。
        if any(r.fatal and r.status is Status.PENDING for r in self.results):
            return "bad"
        if self.warned or self.unverified:
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

    **单阈值模式**：当 normal_v <= low_v 时没有「偏低」档，
    只有 PASS / FAIL 两种结果（>= low_v 通过，否则必须充电）。
    这是 2026-11-01 把交付线定到 11.0 V 后的实际配置，
    在这里写明是为了让调用方知道 WARN 分支在该配置下不可达。
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


def is_single_threshold(normal_v: float, low_v: float) -> bool:
    """电压配置是否处于单阈值模式（没有「偏低提醒」档）。"""
    return float(normal_v) <= float(low_v)


def judge_max_only(value: Optional[float], warn: float, fail: float) -> Status:
    """单侧指标：越低越好（温度、负载、噪音、延迟）。

    正常 → PASS；>= warn → WARN；>= fail → FAIL。
    **低值永远不告警** —— 这正是 2026-11-02 实车踩到的坑：
    用 judge_range(low=0, warn_margin=m) 会把「负载 1.26 < 1.5」
    也判成告警，空载的车被报「指标接近阈值」。

    取不到数据返回 SKIP（不冒充合格）。
    """
    if value is None:
        return Status.SKIP
    try:
        v = float(value)
    except (TypeError, ValueError):
        return Status.SKIP
    if v >= float(fail):
        return Status.FAIL
    if v >= float(warn):
        return Status.WARN
    return Status.PASS


def judge_min_only(value: Optional[float], warn: float, fail: float) -> Status:
    """单侧指标：越高越好（可用内存、信号强度）。

    正常 → PASS；<= warn → WARN；<= fail → FAIL。
    **高值永远不告警**。
    """
    if value is None:
        return Status.SKIP
    try:
        v = float(value)
    except (TypeError, ValueError):
        return Status.SKIP
    if v <= float(fail):
        return Status.FAIL
    if v <= float(warn):
        return Status.WARN
    return Status.PASS


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
    """多次测量取最差判定。

    用于「左右转向各测一次」这类场景：任一次不合格即整体不合格，
    避免单向测了合格就放行（D23）。

    关键：部分 SKIP 意味着「有一项没验证」，不能当通过，
    否则 `[None, 12.0]` 会被判 PASS——审查发现的实际缺陷。
    """
    if not values:
        return Status.SKIP
    statuses = [judge_range(v, low, high, warn_margin) for v in values]
    if any(s is Status.FAIL for s in statuses):
        return Status.FAIL
    if any(s is Status.WARN for s in statuses):
        return Status.WARN
    # 有任何一项没拿到数据，整体就是未验证完
    if any(s is Status.SKIP for s in statuses):
        return Status.SKIP
    return Status.PASS


def judge_all(statuses: List[Status], fail_is_warn: bool = False) -> Status:
    """把多个子项的判定合成一个总判定。

    语义：
      - 任一 FAIL → FAIL（除非 fail_is_warn 降级）
      - 任一 WARN（无 FAIL）→ WARN
      - **部分 PASS + 部分 SKIP → SKIP**（部分验证不算通过）
      - 全 PASS → PASS
      - 全 SKIP → SKIP

    最后一条是审查发现的核心漏洞：之前写的是「任一 PASS 就 PASS」，
    导致 `[PASS, SKIP, SKIP, SKIP]` 被判通过。
    """
    if not statuses:
        return Status.SKIP
    if any(s is Status.FAIL for s in statuses):
        return Status.WARN if fail_is_warn else Status.FAIL
    if any(s is Status.WARN for s in statuses):
        return Status.WARN
    if any(s is Status.SKIP for s in statuses):
        return Status.SKIP
    return Status.PASS
