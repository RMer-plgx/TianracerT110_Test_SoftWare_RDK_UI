"""14. 电池电压。

关键事实：/tianracer/voltage 不是 latched 话题（源码 advertise 时没有 latch 参数），
只在底盘节点初始化后发布一次。订阅晚了就永远读不到。
所以这里给足 90 秒，并记录「启动后第几秒收到」这个延迟。
"""

from __future__ import annotations

import time
from typing import Optional

from core.remote import VOLTAGE_TAP_PATH, ros
from core.verdict import CheckResult, Status, judge_voltage


# 车用电池的合理电压范围。超出这个范围的一定不是电压值。
PLAUSIBLE_MIN_V = 6.0
PLAUSIBLE_MAX_V = 16.0


def _strict_float(text: str) -> Optional[float]:
    """严格解析电压值。

    不能用通用的 parse_float——它会把日志里的时间戳当成电压：
    `2026-10-30 17:15:00 ERROR` → 2026.0，
    `[INFO] [1698700000.123]` → 1698700000.123。
    那样底盘完全不发电压时，报告反而会给出一个「合格」值（审查发现）。

    规则：只接受单行纯数字，且必须在电池合理范围内。
    """
    for line in (text or "").splitlines():
        candidate = line.strip()
        if not candidate or candidate == "---":
            continue
        try:
            value = float(candidate)
        except ValueError:
            continue
        if PLAUSIBLE_MIN_V <= value <= PLAUSIBLE_MAX_V:
            return value
    return None


def run(ctx) -> CheckResult:
    result = CheckResult(number=16, title="电池电压", status=Status.PASS)
    cfg = ctx.baseline.get("voltage", {})
    timeout = int(cfg.get("sample_timeout_sec", 90))

    started = time.time()

    # 先确认底盘节点在不在。不在的话电压话题肯定没发布，
    # 没必要干等 90 秒超时。
    # 用 ros() 前缀而不是手写，保证多网卡时 ROS_IP 也是一致的（Round 2 建议）。
    nodes = ctx.remote.run(ros("rosnode list 2>/dev/null", ctx.profile),
                           timeout=20, record=False).stdout
    # 用 topic() 而不是 topics.get()：漏配 base_node 时要报错，
    # 不能静默跳过底盘预检 —— 那样底盘没起来就只能干等 90 秒超时，
    # 报告写「无数据」而不是「底盘节点未运行」，把人往硬件方向带。
    base_node = ctx.profile.ros.topic("base_node")
    if base_node not in nodes:
        result.status = Status.FAIL
        result.summary = f"底盘节点 {base_node} 未运行，无法读取电压"
        result.detail = (
            "电压话题由底盘节点发布。节点不在，说明串口可能没打开。\n"
            f"检查：{_serial_hint(ctx)}"
        )
        return result

    # 先现订阅。这是唯一可靠的来源——
    # tap 文件可能残留上次运行的旧值（Round 3 审查发现：低电压车会拿到旧值并放行运动）。
    raw = ctx.remote.run(
        ros(f"timeout {timeout} rostopic echo -n 1 "
            f"{ctx.profile.ros.topic('voltage')} 2>&1", ctx.profile),
        timeout=timeout + 15,
    )
    volts = _strict_float(raw.stdout)
    source = "现订阅"
    tap_note = ""

    # 现订阅失败时才看 tap。前提：runner 本次已删旧文件（见 runner._run_inner）。
    if volts is None and getattr(ctx, "owns_bringup", False):
        tapped = ctx.remote.run(f"cat {VOLTAGE_TAP_PATH} 2>/dev/null",
                                timeout=15, record=False).stdout
        volts = _strict_float(tapped)
        if volts is not None:
            source = "启动时抢订阅"
        else:
            tap_note = ("后台抢订阅未取到有效值（"
                        + ("文件不存在" if not tapped.strip() else "文件无电压数值")
                        + "）")

    elapsed = time.time() - started
    result.add("采样耗时", f"{elapsed:.1f}", "s")

    if volts is not None:
        result.add("取值方式", source,
                   note="现订阅更可靠；tap 仅在现订阅失败时作为补充")

    if volts is None:
        # tap_note 只在 volts 为 None 时被赋值，原来放在
        # `if volts is not None` 分支里，永远不可达（Round 4 审查发现）。
        if tap_note:
            result.add("tap 状态", tap_note)
        result.status = Status.FAIL
        result.summary = (f"{timeout} 秒内未收到有效电压值。"
                          f"可能原因：底盘未连接、话题未发布、或上报间隔太长")
        result.detail = (
            "实车采集确认：下位机会主动低频上报电压，间隔极不规律"
            "（20 秒架空采集只看到 3 帧）。\n"
            f"所以 {timeout} 秒未收到不必然是故障 —— 也可能是这轮窗口没碰上。\n"
            "排查顺序：rosclean 后重新 bringup 再试一次；"
            f"仍然读不到就用 debug_cmd_srv 查询，或检查 {_serial_hint(ctx)}"
            + (f"\n后台抢订阅：{tap_note}" if tap_note else "")
        )
        return result

    normal_v = float(cfg.get("normal_v", 11.0))
    low_v = float(cfg.get("low_v", 11.0))
    full_v = float(cfg.get("full_v", 12.4))

    result.add("电压", f"{volts:.2f}", "V",
               expected=f"≥ {normal_v} V（满电 {full_v} V）")

    status = judge_voltage(volts, normal_v, low_v)
    result.status = status

    if status is Status.PASS:
        result.summary = f"{volts:.2f} V，电量充足"
    elif status is Status.WARN:
        result.summary = f"{volts:.2f} V 偏低（交付线 {normal_v} V），建议充电"
    else:
        result.summary = (f"{volts:.2f} V 低于交付线 {low_v} V，必须充电。"
                          f"后续运动测试将被跳过")

    # 电压低时明确告知已阻止运动
    stop_below = float(cfg.get("stop_test_below_v", 11.0))
    if volts < stop_below:
        result.detail = (f"电压低于 {stop_below} V。按文档 7.7，"
                         f"此状态下不得继续带载测试。\n程序会跳过第 18 项运动控制。")
    return result


def _serial_hint(ctx) -> str:
    """拼出「查哪个串口、哪个变量」的提示，从产品配置取，不写死。

    T110 是 /dev/ttyUSB0 + TIANRACER_BASE_PORT，换成别的产品时
    这些值都不同，写死等于给错误指引。
    """
    overrides = getattr(ctx.profile.ros, "env_overrides", {}) or {}

    # 找形如 *_BASE_PORT 的变量
    port_var = next((name for name in overrides if name.endswith("_BASE_PORT")), "")
    port_value = overrides.get(port_var, "") if port_var else ""

    parts = []
    if port_value:
        parts.append(f"sudo lsof {port_value}")
    if port_var:
        parts.append(f"环境变量 {port_var} 是否指向真实设备")
    if not parts:
        return "底盘串口是否被占用，以及环境变量是否指向真实设备"
    return "，以及".join(parts[:1] + ([" ".join(parts[1:])] if len(parts) > 1 else [])) + "。"
