"""17. 系统健康：温度、内存、磁盘、负载。"""

from __future__ import annotations

import re

from core.verdict import CheckResult, Status, judge_range


def run(ctx) -> CheckResult:
    result = CheckResult(number=17, title="系统健康", status=Status.PASS)
    baseline = ctx.baseline.get("health", {})
    r = ctx.remote

    # 温度
    temps_text = r.run(
        "for z in /sys/class/thermal/thermal_zone*/temp; do echo \"$z $(cat $z)\"; done",
        timeout=15,
    )
    temps = []
    for line in temps_text.stdout.splitlines():
        match = re.search(r"(\d{2,})", line.split()[-1] if line.split() else "")
        if match:
            value = int(match.group(1))
            temps.append(value / 1000.0 if value > 1000 else float(value))
    max_temp = max(temps) if temps else None

    warn_c = float(baseline.get("temp_warn_c", 75.0))
    fail_c = float(baseline.get("temp_fail_c", 85.0))
    if max_temp is not None:
        result.add("最高温度", f"{max_temp:.1f}", "°C", expected=f"< {fail_c} °C")
        # 温度是单侧指标（越低越好）。不能用 judge_range(..., low=0,
        # warn_margin=...)：那等于「温度低于 warn_margin 也算告警」，
        # 冬天冷启动读 5°C 就会误报。三段显式判定。
        if max_temp >= fail_c:
            temp_status = Status.FAIL
        elif max_temp >= warn_c:
            temp_status = Status.WARN
        else:
            temp_status = Status.PASS
    else:
        result.add("最高温度", "读取失败")
        temp_status = Status.SKIP

    # 内存
    mem = r.run("free -g | awk '/^Mem:/{print $2, $7}'", timeout=15)
    mem_parts = mem.stdout.split()
    mem_total = mem_avail = None
    if len(mem_parts) >= 2:
        mem_total = parse_int(mem_parts[0])
        mem_avail = parse_int(mem_parts[1])
        result.add("内存可用", mem_avail, "GiB", expected="> 0.5 GiB")
    else:
        result.add("内存", "读取失败")

    # 磁盘
    disk = r.run("df -h / | tail -1", timeout=15)
    disk_pct = None
    match = re.search(r"(\d+)%", disk.stdout)
    if match:
        disk_pct = float(match.group(1))
        disk_warn = float(baseline.get("disk_warn_pct", 85.0))
        disk_fail = float(baseline.get("disk_fail_pct", 95.0))
        result.add("根分区占用", f"{disk_pct:.0f}", "%",
                   expected=f"< {disk_warn:.0f}%",
                   note=f"{disk_warn:.0f}% 警告，{disk_fail:.0f}% 失败")
        # 超过 fail 才是 FAIL，在 warn..fail 之间是 WARN
        if disk_pct >= disk_fail:
            disk_status = Status.FAIL
        elif disk_pct >= disk_warn:
            disk_status = Status.WARN
        else:
            disk_status = Status.PASS
    else:
        result.add("磁盘", "读取失败")
        disk_status = Status.SKIP

    # 负载
    load = r.run("cat /proc/loadavg", timeout=15)
    load1 = parse_float_safe((load.stdout.split() or [""])[0])
    load_warn = float(baseline.get("load_warn", 3.0))
    if load1 is not None:
        result.add("1 分钟负载", f"{load1:.2f}", expected=f"< {load_warn}")
        # 负载同样是单侧指标。原来传 low=0/warn_margin=1.5，
        # 把「负载低于 1.5」也算成告警，导致空载的车被判 WARN
        # （2026-11-02 实测：load 1.26 报「指标接近阈值」）。
        if load1 >= load_warn * 1.5:
            load_status = Status.FAIL
        elif load1 >= load_warn:
            load_status = Status.WARN
        else:
            load_status = Status.PASS
    else:
        result.add("负载", "读取失败")
        load_status = Status.SKIP

    # 高负载来源，便于定位
    top = r.run("ps -eo pcpu,comm --sort=-pcpu | head -4 | tail -3", timeout=15)
    if top.stdout.strip():
        result.add("高 CPU 进程", " / ".join(
            ln.strip() for ln in top.stdout.splitlines() if ln.strip()))

    statuses = [temp_status, disk_status, load_status]
    if mem_avail is not None and mem_avail < baseline.get("mem_available_min_gib", 0.5):
        statuses.append(Status.WARN)
        result.add("内存告警", f"仅剩 {mem_avail} GiB")

    if any(s is Status.FAIL for s in statuses):
        result.status = Status.FAIL
        result.summary = "存在超出安全阈值的指标"
    elif any(s is Status.WARN for s in statuses):
        result.status = Status.WARN
        result.summary = "指标接近阈值（温度/磁盘/负载）"
    elif all(s is Status.SKIP for s in statuses):
        result.status = Status.SKIP
        result.summary = "所有健康指标读取失败"
    else:
        result.summary = (f"温度 {max_temp:.0f}°C，磁盘 {disk_pct:.0f}%，"
                          f"负载 {load1:.2f}" if None not in (max_temp, disk_pct, load1)
                          else "系统指标正常")
    return result


def parse_int(text: str):
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def parse_float_safe(text: str):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None
