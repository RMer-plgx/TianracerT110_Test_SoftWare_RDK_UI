"""14. 电池电压。

关键事实：/tianracer/voltage 不是 latched 话题（源码 advertise 时没有 latch 参数），
只在底盘节点初始化后发布一次。订阅晚了就永远读不到。
所以这里给足 90 秒，并记录「启动后第几秒收到」这个延迟。
"""

from __future__ import annotations

import time

from core.verdict import CheckResult, Status, judge_voltage

from ._util import parse_float


def run(ctx) -> CheckResult:
    result = CheckResult(number=14, title="电池电压", status=Status.PASS)
    cfg = ctx.baseline.get("voltage", {})
    timeout = int(cfg.get("sample_timeout_sec", 90))

    started = time.time()

    # 先确认底盘节点在不在。不在的话电压话题肯定没发布，
    # 没必要干等 90 秒超时。
    nodes = ctx.remote.run(
        "source /opt/ros/noetic/setup.bash >/dev/null 2>&1; "
        "source ~/tianracer_ros1_ws/devel/setup.bash >/dev/null 2>&1; "
        "rosnode list 2>/dev/null",
        timeout=20, record=False,
    ).stdout
    if "/tianracer/tianracer" not in nodes:
        result.status = Status.FAIL
        result.summary = "底盘节点 /tianracer/tianracer 未运行，无法读取电压"
        result.detail = (
            "电压话题由底盘节点发布。节点不在，说明串口可能没打开。\n"
            "检查：sudo lsof /dev/ttyUSB0，以及环境变量 TIANRACER_BASE_PORT 是否指向真实设备。"
        )
        return result

    raw = ctx.remote.run(
        "source /opt/ros/noetic/setup.bash >/dev/null 2>&1; "
        "source ~/tianracer_ros1_ws/devel/setup.bash >/dev/null 2>&1; "
        f"timeout {timeout} rostopic echo -n 1 /tianracer/voltage 2>&1",
        timeout=timeout + 15,
    )
    elapsed = time.time() - started
    volts = parse_float(raw.stdout)

    result.add("采样耗时", f"{elapsed:.1f}", "s",
               note="该话题只发布一次，订阅晚了读不到")

    if volts is None:
        result.status = Status.FAIL
        result.summary = (f"{timeout} 秒内未收到电压消息。"
                          f"可能原因：底盘未连接、话题已错过发布时刻")
        result.detail = (
            "rosclean 后重新启动 bringup 再试一次。\n"
            "如果仍然读不到，用 debug_cmd_srv 查询，或检查 /dev/ttyUSB0 是否被占用。"
        )
        return result

    normal_v = float(cfg.get("normal_v", 11.7))
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
        result.summary = (f"{volts:.2f} V 低于 {low_v} V，必须充电。"
                          f"后续运动测试将被跳过")

    # 电压低时明确告知已阻止运动
    stop_below = float(cfg.get("stop_test_below_v", 11.0))
    if volts < stop_below:
        result.detail = (f"电压低于 {stop_below} V。按文档 7.7，"
                         f"此状态下不得继续带载测试。\n程序会跳过第 18 项运动控制。")
    return result
