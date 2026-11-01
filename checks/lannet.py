"""10. 雷达以太网链路。"""

from __future__ import annotations

import re

from core.verdict import CheckResult, Status


def run(ctx) -> CheckResult:
    result = CheckResult(number=10, title="雷达以太网", status=Status.PASS)
    baseline = ctx.baseline.get("network", {})
    lidar_ip = baseline.get("lidar_ip", "192.168.198.2")
    prefix = baseline.get("lidar_host_ip_prefix", "192.168.198.")
    count = int(baseline.get("ping_count", 3))
    r = ctx.remote

    addr = r.run("ip -4 addr show eth0 2>&1 | grep 'inet '", timeout=15).stdout.strip()
    result.add("eth0 地址", addr or "未配置")
    if prefix not in addr:
        result.status = Status.FAIL
        result.summary = f"eth0 未配置 {prefix}x 网段地址，雷达无法通信"
        return result

    ping = r.run(f"ping -c {count} -W 1 {lidar_ip} 2>&1", timeout=25)
    loss = None
    rtt = None
    match = re.search(r"(\d+(?:\.\d+)?)% packet loss", ping.stdout)
    if match:
        loss = float(match.group(1))
    rtt_match = re.search(r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)", ping.stdout)
    if rtt_match:
        rtt = float(rtt_match.group(2))

    max_loss = float(baseline.get("ping_max_loss_pct", 0))
    max_rtt = float(baseline.get("ping_max_rtt_ms", 5.0))

    result.add("丢包率", loss if loss is not None else "无响应", "%",
               expected=f"≤ {max_loss:.0f}%")
    if rtt is not None:
        result.add("平均延迟", f"{rtt:.2f}", "ms", expected=f"≤ {max_rtt} ms")

    if loss is None:
        result.status = Status.FAIL
        result.summary = f"ping {lidar_ip} 失败，雷达离线或网线未接"
    elif loss > max_loss:
        result.status = Status.FAIL
        result.summary = f"丢包率 {loss:.0f}%"
    elif rtt is not None and rtt > max_rtt:
        result.status = Status.WARN
        result.summary = f"延迟偏高 {rtt:.1f} ms"
    else:
        result.summary = f"{lidar_ip} 可达，丢包 {loss:.0f}%，延迟 {rtt:.2f} ms" if rtt else f"{lidar_ip} 可达"

    return result
