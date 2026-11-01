"""19. 使用痕迹。

只报告，不判失败。这些数据是交付前清理的参考。
"""

from __future__ import annotations

import re

from core.verdict import CheckResult, Status


def run(ctx) -> CheckResult:
    result = CheckResult(number=19, title="使用痕迹", status=Status.PASS)
    baseline = ctx.baseline.get("traces", {})
    r = ctx.remote

    log_size = r.run("du -sm ~/.ros/log 2>/dev/null | cut -f1", timeout=40)
    size_mb = _to_int(log_size.stdout)
    log_dirs = r.run("ls ~/.ros/log 2>/dev/null | wc -l", timeout=20)
    dir_count = _to_int(log_dirs.stdout)

    warn_mb = int(baseline.get("ros_log_size_warn_mb", 200))
    result.add("ROS 日志体积", size_mb if size_mb is not None else "读取失败", "MB",
               expected=f"< {warn_mb} MB")
    result.add("ROS 日志目录数", dir_count if dir_count is not None else "?")

    hist = r.run("wc -l < ~/.bash_history 2>/dev/null || echo 0", timeout=20)
    hist_lines = _to_int(hist.stdout)
    warn_lines = int(baseline.get("bash_history_lines_warn", 800))
    # 读不到就写「读取失败」，不要写成 0 行——那会让报告看起来像干净的
    result.add("bash_history",
               hist_lines if hist_lines is not None else "读取失败",
               "行" if hist_lines is not None else "",
               expected=f"< {warn_lines} 行")

    viminfo = r.run("ls -l ~/.viminfo 2>/dev/null | wc -l", timeout=15)
    result.add(".viminfo", "存在" if (viminfo.stdout.strip() or "0") != "0" else "不存在")

    result.add("git 历史", "保留（不应清空）",
               note="git log 属仓库本身，交付前只需确认 status 干净")

    tips = []
    if size_mb is not None and size_mb >= warn_mb:
        tips.append(f"ROS 日志 {size_mb} MB 偏大，建议 rosclean purge")
    if hist_lines is not None and hist_lines >= warn_lines:
        tips.append(f"bash_history {hist_lines} 行，交付前可清理")

    result.status = Status.WARN if tips else Status.PASS
    result.summary = "；".join(tips) if tips else "痕迹量正常（仅报告，不判定失败）"
    return result


def _to_int(text: str):
    match = re.search(r"\d+", text or "")
    return int(match.group(0)) if match else None
