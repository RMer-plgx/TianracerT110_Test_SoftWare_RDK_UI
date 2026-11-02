"""19. 使用痕迹。

两件事：
- run():     测试时检查痕迹现状，写进报告
- cleanup(): 整轮测试结束后自动删除痕迹（用户 2026-11-02 要求）

清理必须在 roslaunch 停掉之后、SSH 断开之前做，所以不放在 run() 里，
由 runner 的收尾流程调用。
"""

from __future__ import annotations

import re

from core.verdict import CheckResult, Status


# 清理脚本。先量再删，量不到也不报错（痕迹本来就可能不存在）。
# 用 find -mindepth 1 而不是 rm -rf dir/*，后者漏掉隐藏文件。
CLEAN_SCRIPT = r'''
H="$HOME"

before_mb=$(du -sm "$H/.ros/log" 2>/dev/null | cut -f1); before_mb=${before_mb:-0}
before_dirs=$(ls "$H/.ros/log" 2>/dev/null | wc -l); before_dirs=${before_dirs:-0}
before_hist=$(wc -l < "$H/.bash_history" 2>/dev/null); before_hist=${before_hist:-0}

if [ -d "$H/.ros/log" ]; then
  find "$H/.ros/log" -mindepth 1 -exec rm -rf {} + 2>/dev/null
fi

: > "$H/.bash_history" 2>/dev/null
rm -f "$H"/.bash_history-* 2>/dev/null
rm -f "$H/.viminfo" 2>/dev/null

sync 2>/dev/null

after_mb=$(du -sm "$H/.ros/log" 2>/dev/null | cut -f1); after_mb=${after_mb:-0}
after_dirs=$(ls "$H/.ros/log" 2>/dev/null | wc -l); after_dirs=${after_dirs:-0}
after_hist=$(wc -l < "$H/.bash_history" 2>/dev/null); after_hist=${after_hist:-0}
after_vim=$(ls -l "$H/.viminfo" 2>/dev/null | wc -l); after_vim=${after_vim:-0}

echo "LOGS_BEFORE_MB=$before_mb"
echo "LOGS_BEFORE_DIRS=$before_dirs"
echo "HIST_BEFORE=$before_hist"
echo "LOGS_AFTER_MB=$after_mb"
echo "LOGS_AFTER_DIRS=$after_dirs"
echo "HIST_AFTER=$after_hist"
echo "VIM_AFTER=$after_vim"
'''


def cleanup(ctx) -> dict:
    """删除车上使用痕迹。返回解析后的字典（键名小写）。

    失败不抛异常——清理失败不应该让整轮测试算失败，
    但返回里会带 error 字段，报告里会写清楚。
    """
    try:
        out = ctx.remote.run(CLEAN_SCRIPT, timeout=120, record=True)
    except Exception as exc:  # 连接断了、超时都不能让收尾崩掉
        return {"error": f"{type(exc).__name__}: {exc}"}
    if not out.ok:
        return {"error": (out.stderr or "清理命令返回非零").strip()[:200]}

    data = {}
    for line in (out.stdout or "").splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        if re.fullmatch(r"[a-z_]+", key):
            data[key] = _to_int(value) if value.strip().isdigit() else value.strip()
    return data


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
