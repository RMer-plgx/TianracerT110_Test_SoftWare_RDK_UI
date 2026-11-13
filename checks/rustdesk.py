"""3. RustDesk 远程桌面。

## 黑屏的真正原因（2026-11-13 实测，ADR-0011）

用户原先以为是「缺 HDMI 诱骗器」。实测推翻了：真正的原因是
**显示服务器没起来**，而 `headless-display.service` 启动失败：

    ExecStartPre=/usr/bin/xauth ... add :0 . $(mcookie)   → exit 1
    报错：key contains odd number of or non-hex characters

systemd 的 `ExecStartPre=` **不做 shell 命令替换**，`$(mcookie)` 被当作
字面量传给 xauth。同一条命令在 `bash -c` 里跑就 exit=0。

连锁反应：

    headless-display 失败 → 没有 :0 → rustdesk --server 起不来 → 远程黑屏

## 为什么不用 Xvfb（headless-display 那条路）

实测：修好 `headless-display.service` 后 Xvfb 能起来、有 1920x1080，
**但 rustdesk --server 不出现** —— Xvfb 是纯软件渲染、没有桌面会话，
RustDesk 抓不到屏。

所以正确路径是 **lightdm + Xorg**（配合 `/etc/X11/xorg.conf.d/20-virtual.conf`），
它提供真实的 X 会话 + autologin xfce，RustDesk 能正常工作。

## 自动修复

车端已加 `ensure-x-display.service` 保证开机有 X。本项作为**兜底**：
若 X 仍未起来，检测软件自己尝试拉起，不让用户输命令。

修复顺序：
1. `systemctl start lightdm`（配合 20-virtual.conf）
2. 等待 X socket 出现（每 2 秒查一次，最多 15 秒）
3. `systemctl start rustdesk`
4. 等待 `--server` 出现

全程不弹对话框。成功则判 PASS，失败才 WARN 并给出物理动作建议。
"""

from __future__ import annotations

import time

from core.verdict import CheckResult, Status

# 修复窗口：每 2 秒查一次，最多 15 秒
POLL_INTERVAL = 2.0
POLL_MAX_SECONDS = 15.0


def _snapshot(r):
    """一次性取全部状态，减少 SSH 往返。"""
    out = r.run(
        "echo SERVICE=$(systemctl is-active rustdesk 2>&1); "
        "echo ENABLED=$(systemctl is-enabled rustdesk 2>&1); "
        "echo XSOCK=$(ls /tmp/.X11-unix/ 2>/dev/null | tr '\\n' ',' ); "
        "echo SESSION=$(who 2>/dev/null | head -1)",
        timeout=20, record=False,
    ).stdout
    vals = {}
    for line in (out or "").splitlines():
        key, sep, val = line.partition("=")
        if sep:
            vals[key.strip()] = val.strip()
    return vals


def _procs(r):
    """RustDesk 三种进程是否存在。"""
    out = r.run("ps -ef | grep -i rustdesk | grep -v grep",
                timeout=20, record=False).stdout or ""
    return {
        "service": "--service" in out,
        "server": "--server" in out,
        "tray": "--tray" in out,
    }


def _has_x(vals) -> bool:
    return bool((vals.get("XSOCK") or "").strip(",").strip())


def _wait_for_x(r, ctx, deadline: float) -> bool:
    """等 :0 出现。返回是否等到。"""
    while time.time() < deadline:
        if _has_x(_snapshot(r)):
            return True
        if getattr(ctx, "cancel_requested", False):
            return False
        time.sleep(POLL_INTERVAL)
    return _has_x(_snapshot(r))


def _try_repair(r, ctx) -> list:
    """尝试把显示服务器拉起来。返回实际执行的命令（写进报告）。"""
    ran = []
    password = getattr(ctx, "sudo_password", "ros")
    say = getattr(ctx, "say", None)

    def log(msg):
        if say:
            try:
                say(f"       {msg}")
            except Exception:
                pass

    log("检测到没有 X 会话，尝试自动修复…")

    # 1. lightdm（配合 20-virtual.conf 提供虚拟输出）
    res = r.sudo("systemctl start lightdm", password, timeout=60)
    ran.append("systemctl start lightdm")
    if not res.ok:
        log(f"lightdm 启动返回非 0（{res.exit_code}），继续观察")

    # 2. 等 X socket
    deadline = time.time() + POLL_MAX_SECONDS
    if _wait_for_x(r, ctx, deadline):
        log("X 会话已就绪")
    else:
        log(f"{int(POLL_MAX_SECONDS)} 秒内 X 仍未起来")

    # 3. rustdesk
    vals = _snapshot(r)
    if vals.get("SERVICE") != "active":
        r.sudo("systemctl start rustdesk", password, timeout=60)
        ran.append("systemctl start rustdesk")
    # 给 rustdesk 一点时间拉起 --server
    for _ in range(int(POLL_MAX_SECONDS // POLL_INTERVAL)):
        if _procs(r)["server"]:
            break
        time.sleep(POLL_INTERVAL)

    return ran


def run(ctx) -> CheckResult:
    result = CheckResult(number=3, title="RustDesk 远程桌面", status=Status.PASS)
    r = ctx.remote

    vals = _snapshot(r)
    procs = _procs(r)

    active = vals.get("SERVICE", "")
    enabled = vals.get("ENABLED", "")
    has_desktop = _has_x(vals) and bool(vals.get("SESSION"))
    repaired = []

    # 自动修复默认开（用户要求「零操作」），但可以由产品配置关掉。
    # 这是检测工具**主动改变车端运行状态**的唯一一处，应该由配置声明。
    cfg = (ctx.baseline or {}).get("rustdesk", {})
    auto_repair = bool(cfg.get("auto_repair", True))

    # ---- 自动修复：只在「服务在跑但没有图形会话」时才动手 ----
    if auto_repair and active == "active" and not has_desktop:
        repaired = _try_repair(r, ctx)
        vals = _snapshot(r)
        procs = _procs(r)
        active = vals.get("SERVICE", "")
        enabled = vals.get("ENABLED", "")
        has_desktop = _has_x(vals) and bool(vals.get("SESSION"))

    result.add("服务状态", active or "未知", expected="active")
    result.add("开机自启", enabled or "未知", expected="enabled")
    result.add("--service 进程", "存在" if procs["service"] else "缺失")
    result.add("--server 进程", "存在" if procs["server"] else "缺失",
               note="缺此项远程会黑屏")
    result.add("--tray 进程", "存在" if procs["tray"] else "缺失")
    result.add("图形会话", vals.get("SESSION") or "缺失（无 :0 会话）")
    if repaired:
        result.add("自动修复", "已执行 " + "、".join(repaired),
                   note="检测发现没有图形会话时自动拉起，未修改车辆文件")
        for cmd in repaired:
            result.add_command(cmd)

    rid = r.run("rustdesk --get-id 2>&1", timeout=15, record=False).stdout.strip()
    if rid and rid.isdigit():
        result.add("RustDesk ID", rid)

    # ---- 判定 ----
    if active != "active":
        result.status = Status.FAIL
        result.summary = "RustDesk 服务未运行"
        result.detail = (
            "systemctl is-active rustdesk 返回的不是 active。\n"
            "尝试：sudo systemctl start rustdesk\n"
            "若反复失败，检查 journalctl -u rustdesk 的输出。"
        )
        return result

    if not has_desktop:
        result.status = Status.WARN
        if auto_repair:
            result.summary = "没有图形会话（X 未启动），远程连接会黑屏"
            tried = "检测软件已尝试自动拉起 lightdm，但仍失败。"
        else:
            result.summary = "没有图形会话（X 未启动），远程连接会黑屏"
            tried = ("本项已配置为不自动修复（baseline.yaml 的 "
                     "`rustdesk.auto_repair: false`），未尝试拉起 lightdm。")
        result.detail = (
            "这不是显示器的问题：" + tried + "\n"
            "\n"
            "按可能性排序：\n"
            "  1. HDMI 诱骗器没插紧 —— 拔下重新插一遍（最常见）\n"
            "  2. lightdm 起不来 —— sudo systemctl status lightdm 看原因\n"
            "  3. 没有任何显示输出 —— 本车型需要诱骗器或真实显示器\n"
            "\n"
            "注意：插拔诱骗器只在 X 已经在运行时才有效。\n"
            "如果上面第 2 条显示 lightdm 是 failed，插拔也不会管用。"
        )
        return result

    if not procs["server"]:
        result.status = Status.WARN
        result.summary = "X 已就绪但 RustDesk 未接管（缺 --server），远程会黑屏"
        result.detail = (
            "图形会话存在，但 rustdesk --server 没起来。\n"
            "尝试：sudo systemctl restart rustdesk\n"
            "本项在检测时已自动尝试过，恢复约需 5~10 秒。"
        )
        return result

    if repaired:
        # 明确写出来：报告读者要知道「这台车检测前是没画面的，
        # 是检测软件把它拉起来的」，而不是以为本来就正常。
        result.summary = ("服务与用户会话均正常"
                          "（本次检测自动启动了 " + "、".join(
                              x.replace("systemctl start ", "") for x in repaired) + "）")
    else:
        result.summary = "服务与用户会话均正常"
    return result
