"""17. 系统健康：温度、内存、磁盘、负载。

报告措辞要求（用户 2026-11-03）：
- 通过时必须写出温度/磁盘/负载三个具体值，不能只说「正常」
- 未通过时必须指出**哪一项、实测多少、超过哪个阈值**，
  不能笼统地说「指标接近阈值」

所以这里不再用固定文案，而是把每一项的判定结果收进
`_Finding` 列表，最后由 `_summary()` 统一拼句子。
"""

from __future__ import annotations

import re

from core.verdict import CheckResult, Status, judge_max_only


class _Finding:
    """单项指标的判定记录，用于拼出可读的结论句。"""

    __slots__ = ("name", "text", "status", "limit", "limit_text")

    def __init__(self, name: str, text: str, status: Status,
                 limit: float | None = None, limit_text: str = ""):
        self.name = name
        self.text = text          # 实测值的可读形式，例如 "40.0 °C"
        self.status = status
        self.limit = limit        # 对应阈值（数值），用于算超出多少
        self.limit_text = limit_text  # 阈值的可读形式，例如 "75 °C"

    def sentence(self) -> str:
        """生成「温度 80.0 °C 超过 75 °C」这样的句子。"""
        if self.status is Status.FAIL:
            word = "超过失败线"
        elif self.status is Status.WARN:
            word = "超过警告线"
        else:
            word = "在阈值内"
        if self.limit_text:
            return f"{self.name} {self.text} {word} {self.limit_text}"
        return f"{self.name} {self.text}"


def run(ctx) -> CheckResult:
    result = CheckResult(number=17, title="系统健康", status=Status.PASS)
    baseline = ctx.baseline.get("health", {})
    r = ctx.remote
    findings: list[_Finding] = []

    # ---- 温度 ----
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
        result.add("最高温度", f"{max_temp:.1f}", "°C",
                   expected=f"< {warn_c:.0f} °C 警告，< {fail_c:.0f} °C 失败")
        # 单侧指标：低值永远不告警（冬天冷启动读 5°C 不该报）
        st = judge_max_only(max_temp, warn_c, fail_c)
        limit, limit_text = (
            (fail_c, f"{fail_c:.0f} °C") if st is Status.FAIL
            else (warn_c, f"{warn_c:.0f} °C")
        )
        findings.append(_Finding("温度", f"{max_temp:.1f} °C", st, limit, limit_text))
    else:
        result.add("最高温度", "读取失败")
        findings.append(_Finding("温度", "读取失败", Status.SKIP))

    # ---- 内存 ----
    mem = r.run("free -g | awk '/^Mem:/{print $2, $7}'", timeout=15)
    mem_parts = mem.stdout.split()
    mem_total = mem_avail = None
    mem_min = float(baseline.get("mem_available_min_gib", 0.5))
    if len(mem_parts) >= 2:
        mem_total = parse_int(mem_parts[0])
        mem_avail = parse_int(mem_parts[1])
        result.add("内存可用", mem_avail, "GiB", expected=f"> {mem_min} GiB")
        if mem_avail is not None:
            mem_status = Status.PASS if mem_avail > mem_min else Status.WARN
            if mem_status is not Status.PASS:
                findings.append(_Finding(
                    "可用内存", f"{mem_avail} GiB", mem_status,
                    mem_min, f"{mem_min} GiB",
                ))
    else:
        result.add("内存", "读取失败")

    # ---- 磁盘 ----
    disk = r.run("df -h / | tail -1", timeout=15)
    disk_pct = None
    match = re.search(r"(\d+)%", disk.stdout)
    disk_warn = float(baseline.get("disk_warn_pct", 85.0))
    disk_fail = float(baseline.get("disk_fail_pct", 95.0))
    if match:
        disk_pct = float(match.group(1))
        result.add("根分区占用", f"{disk_pct:.0f}", "%",
                   expected=f"< {disk_warn:.0f}% 警告，< {disk_fail:.0f}% 失败")
        st = judge_max_only(disk_pct, disk_warn, disk_fail)
        limit, limit_text = (
            (disk_fail, f"{disk_fail:.0f}%") if st is Status.FAIL
            else (disk_warn, f"{disk_warn:.0f}%")
        )
        findings.append(_Finding("磁盘", f"{disk_pct:.0f}%", st, limit, limit_text))
    else:
        result.add("磁盘", "读取失败")
        findings.append(_Finding("磁盘", "读取失败", Status.SKIP))

    # ---- 负载 ----
    load = r.run("cat /proc/loadavg", timeout=15)
    load1 = parse_float_safe((load.stdout.split() or [""])[0])
    load_warn = float(baseline.get("load_warn", 3.0))
    load_fail = load_warn * 1.5
    if load1 is not None:
        result.add("1 分钟负载", f"{load1:.2f}",
                   expected=f"< {load_warn:.1f} 警告，< {load_fail:.1f} 失败")
        st = judge_max_only(load1, load_warn, load_fail)
        limit, limit_text = (
            (load_fail, f"{load_fail:.1f}") if st is Status.FAIL
            else (load_warn, f"{load_warn:.1f}")
        )
        findings.append(_Finding("负载", f"{load1:.2f}", st, limit, limit_text))
    else:
        result.add("负载", "读取失败")
        findings.append(_Finding("负载", "读取失败", Status.SKIP))

    # ---- 高负载来源，便于定位 ----
    top = r.run("ps -eo pcpu,comm --sort=-pcpu | head -4 | tail -3", timeout=15)
    if top.stdout.strip():
        result.add("高 CPU 进程", " / ".join(
            ln.strip() for ln in top.stdout.splitlines() if ln.strip()))

    # ---- 汇总 ----
    three = [f for f in findings if f.name in ("温度", "磁盘", "负载")]
    statuses = [f.status for f in three]
    if mem_avail is not None and mem_avail <= mem_min:
        statuses.append(Status.WARN)

    bad = [f for f in findings if f.status is Status.FAIL]
    warn = [f for f in findings if f.status is Status.WARN]
    ok = [f for f in findings if f.status is Status.PASS]

    if bad:
        result.status = Status.FAIL
        result.summary = "；".join(f.sentence() for f in bad)
        if warn or ok:
            result.detail = "其余指标：\n" + "\n".join(
                f"  - {f.sentence()}" for f in warn + ok)
    elif warn:
        result.status = Status.WARN
        result.summary = "；".join(f.sentence() for f in warn)
        if ok:
            result.detail = "正常项：\n" + "\n".join(
                f"  - {f.name} {f.text}" for f in ok)
    elif all(s is Status.SKIP for s in statuses):
        result.status = Status.SKIP
        result.summary = "温度、磁盘、负载三项均读取失败"
    elif not ok:
        # 三项都没取到（全 SKIP 已在上面处理），这里兜底
        result.status = Status.SKIP
        result.summary = "健康指标未取得有效数据"
    else:
        result.status = Status.PASS
        # 通过时也把三个具体值写出来 —— 用户要看到实测数字，不是「正常」
        parts = [f"{f.name} {f.text}" for f in ok]
        result.summary = "，".join(parts)
        if len(ok) < 3:
            missing = [n for n in ("温度", "磁盘", "负载")
                       if all(f.name != n for f in ok)]
            if missing:
                result.summary += "；未取得：" + "、".join(missing)
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
