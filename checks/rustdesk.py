"""3. RustDesk 远程桌面。

本平台已弃用 VNC。判断标准是「服务 active」且「用户会话进程存在」，
只有 --service 没有 --server 时远程会看到黑屏。
"""

from __future__ import annotations

import re

from core.verdict import CheckResult, Status


def run(ctx) -> CheckResult:
    result = CheckResult(number=3, title="RustDesk 远程桌面", status=Status.PASS)
    r = ctx.remote

    active = r.run("systemctl is-active rustdesk 2>&1", timeout=15).stdout.strip()
    enabled = r.run("systemctl is-enabled rustdesk 2>&1", timeout=15).stdout.strip()
    result.add("服务状态", active, expected="active")
    result.add("开机自启", enabled, expected="enabled")

    procs = r.run("ps -ef | grep -i rustdesk | grep -v grep", timeout=15).stdout
    has_service = "--service" in procs
    has_server = "--server" in procs
    has_tray = "--tray" in procs

    result.add("--service 进程", "存在" if has_service else "缺失")
    result.add("--server 进程", "存在" if has_server else "缺失",
               note="缺此项远程会黑屏")
    result.add("--tray 进程", "存在" if has_tray else "缺失")

    session = r.run("who 2>&1", timeout=15).stdout
    has_desktop = bool(re.search(r"tty[0-9].*:0", session))
    result.add("图形会话", "存在" if has_desktop else "缺失（无 :0 会话）")

    rid = r.run("rustdesk --get-id 2>&1", timeout=15).stdout.strip()
    if rid and rid.isdigit():
        result.add("RustDesk ID", rid)

    if active != "active":
        result.status = Status.FAIL
        result.summary = "RustDesk 服务未运行"
    elif not has_server:
        result.status = Status.WARN
        result.summary = "仅 --service 在运行，远程连接会黑屏"
    elif not has_desktop:
        result.status = Status.WARN
        result.summary = "缺少图形会话，远程可能黑屏"
    else:
        result.summary = "服务与用户会话均正常"

    return result
