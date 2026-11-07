#!/usr/bin/env python3
"""协议层采集工具 —— 手动跑一次「看下位机到底在发什么」。

用途
----
不接 GUI，纯命令行验证：把 serial_tap.py 送到车上，用 pty 接管底盘串口，
采一段原始字节，取回本地解析，打印下位机真实在发什么。

这是接进 GUI 流程**之前**的验证步骤。先确认「pty 桥接不影响底盘正常工作、
帧长公式与固件一致、能解出电压」，再谈集成。

用法
----
    python tools/tap_probe.py --host 192.168.128.10 --seconds 120

安全（必读）
------------
- ⚠️ **采集期间车辆必须架空且不得运动。** 桥接是控制链路上的单点：
  本工具退出后，底盘节点已发给 MCU 的运动指令**不会自动撤销**。
  停车靠 MCU 固件的通信超时保护 —— 已于 2026-11-07 两次实测确认
  （实测 0.4~2.1 秒内停住车），但**属固件特性**，换车型/固件需重做实验。
  因此架空仍是硬约束，不因上述结论放宽。
- 全程 try/finally：无论怎么退出，都停 bringup、停 tap
- 同时监视两种死亡：进程消失（`pgrep`）与**循环停摆**
  （`TAP_STATS` 不再刷新）。后者进程还活着，只靠 `pgrep` 发现不了
- 只读不写：本工具不发送任何运动指令
- 电压帧上报极稀疏（实车 20 秒仅 3 帧），要稳定观测请用 ≥120 秒

退出码：0 正常，1 参数/连接错误，2 车辆侧失败，3 解析失败
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.protocol import FrameParser, ParseStats          # noqa: E402
from core.profile import load as load_profile              # noqa: E402
from core.remote import source_only                        # noqa: E402
from core.ssh import Remote, SSHError                      # noqa: E402

TAP_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "probes", "serial_tap.py")
REMOTE_TAP = "/tmp/serial_tap.py"
TAP_OUT = "/tmp/tap.out"

# TAP_STATS 每 5 秒一条，2 秒轮询。连续这么多次（即 10 秒）计数不动
# 就判定转发停摆 —— 宁可误报也不敢让卡死的桥接继续挂着。
STALL_LIMIT = 5


def _last_stats_progress(out: str):
    """从 tap 输出里取最后一行的转发计数。

    返回 (to_mcu, from_mcu)；还没出现可解析的 TAP_STATS 时返回 None。

    解析失败的那一行要**跳过继续往前找**，不能直接放弃：输出文件可能
    恰好读到半行（写入中），如果 return None，上层只能 continue，
    stalled 既不增也不清 —— 停滞检测就永久失效了。
    """
    for line in reversed(out.splitlines()):
        if not line.startswith("TAP_STATS "):
            continue
        try:
            stats = json.loads(line.split(" ", 1)[1])
        except (ValueError, IndexError):
            continue
        if not isinstance(stats, dict):
            continue
        return (stats.get("to_mcu", 0), stats.get("from_mcu", 0))
    return None


def env_exports(profile, overrides: dict) -> str:
    """把 profile 的 env_overrides 拼成 export 语句，再用 overrides 覆盖。

    工作空间、启动包、环境变量全部从 profile 取 —— 这个工具没有任何
    产品专属字符串，换产品只改 profile。
    """
    merged = dict(getattr(profile.ros, "env_overrides", {}) or {})
    merged.update(overrides)
    parts = [f"export {key}={value}" for key, value in merged.items()]
    return "; ".join(parts) + "; " if parts else ""


def log(msg: str) -> None:
    print(msg, flush=True)


def parse_ready(out: str) -> str:
    """从 tap 的 stdout 里取 pty 路径。"""
    for line in out.splitlines():
        if line.startswith("TAP_READY "):
            return line.split(" ", 1)[1].strip()
    return ""


def kill_tap(remote: Remote) -> None:
    remote.run("pkill -TERM -f serial_tap.py 2>/dev/null; sleep 1; "
               "pkill -9 -f serial_tap.py 2>/dev/null; true", record=False)


def stop_launch(remote: Remote, profile) -> None:
    package = profile.ros.launch_package
    remote.run(f"pkill -INT -f 'roslaunch {package}' 2>/dev/null; "
               f"sleep 3; pkill -9 -f 'roslaunch {package}' 2>/dev/null; "
               "pkill -9 -f rosmaster 2>/dev/null; true", record=False)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="协议层采集与解析",
        epilog="⚠ 采集期间车辆必须架空且不得运动。桥接退出后，底盘节点"
               "已发出的运动指令不会自动撤销；停车靠 MCU 通信超时保护"
               "（已实测确认 0.4~2.1 秒，但属固件特性）。",
    )
    ap.add_argument("--host", default="192.168.128.10")
    ap.add_argument("--user", default="sunrise")
    ap.add_argument("--password", default="sunrise")
    ap.add_argument("--seconds", type=float, default=20.0,
                    help="采集时长。电压帧上报极稀疏，建议 ≥120 秒。")
    ap.add_argument("--profile", default=None,
                    help="产品 id，缺省用默认产品")
    ap.add_argument("--port", default=None,
                    help="真实串口，缺省从 profile 的 *_BASE_PORT 取")
    ap.add_argument("--baud", type=int, default=None,
                    help="波特率，缺省从 profile 的 *_BASE_BAUDRATE 取")
    ap.add_argument("--keep-raw", action="store_true",
                    help="把原始日志存到本地，便于事后复查")
    args = ap.parse_args()

    profile = load_profile(args.profile)
    # 日志目录从 profile 取，与 checks/protocol.py 用同一个来源。
    # 两边各写一份硬编码迟早会分叉。
    tap_dir = profile.ros.tap.log_dir
    overrides = getattr(profile.ros, "env_overrides", {}) or {}

    # 串口与波特率未显式指定时，从同一个 env_overrides 取，
    # 避免工具里再写一份设备名 / 波特率。
    if args.port is None:
        port_var = next((k for k in overrides if k.endswith("_BASE_PORT")), "")
        args.port = overrides.get(port_var, "/dev/ttyUSB0")
    if args.baud is None:
        baud_var = next((k for k in overrides if k.endswith("_BASE_BAUDRATE")), "")
        args.baud = int(overrides.get(baud_var, 115200))

    with open(TAP_SCRIPT, "r", encoding="utf-8") as fh:
        tap_source = fh.read()

    # 运行时也必须提醒一次 —— help 里的文字安装完就没人看了。
    log("⚠  安全约束：采集期间车辆必须架空且不得运动。")
    log("   桥接退出后，底盘节点已发出的运动指令不会自动撤销；")
    log("   停车靠 MCU 通信超时保护（已实测确认 0.4~2.1 秒，属固件特性）。")

    remote = Remote(args.host, args.user, args.password)
    pty_path = ""
    try:
        remote.connect()
        log(f"[1/6] 已连接 {args.host}")

        # --- 清场 ---
        stop_launch(remote, profile)
        kill_tap(remote)
        remote.run(f"rm -rf {tap_dir} {TAP_OUT}; mkdir -p {tap_dir}", record=False)

        # --- 上传并启动 tap ---
        remote.put_script(tap_source, REMOTE_TAP)
        remote.run(
            f"chmod +x {REMOTE_TAP}; "
            f"setsid nohup python3 {REMOTE_TAP} --port {args.port} "
            f"--baud {args.baud} --out {tap_dir} "
            f"> {TAP_OUT} 2>&1 < /dev/null & echo started",
            record=False,
        )
        log("[2/6] 桥接已启动，等待 pty 就绪…")

        deadline = time.time() + 15
        while time.time() < deadline:
            out = remote.read_file(TAP_OUT)
            pty_path = parse_ready(out)
            if pty_path:
                break
            if "TAP_ERROR" in out:
                log("[!] 桥接报错：")
                log(out)
                return 2
            time.sleep(0.5)

        if not pty_path:
            log("[!] 15 秒内没等到 TAP_READY")
            log(remote.read_file(TAP_OUT))
            return 2
        log(f"[3/6] pty 就绪：{pty_path}")

        # --- 用 pty 启 bringup ---
        # source 前缀与启动包名都来自 profile，工具里不写死产品信息
        launch = (
            source_only(profile)
            + env_exports(profile, {
                # 唯一的关键差异：把底盘串口指向 pty，让帧流经过桥接
                next((k for k in overrides if k.endswith("_BASE_PORT")),
                     "BASE_PORT"): pty_path,
            })
            + f"cd ~; setsid nohup roslaunch "
            + f"{profile.ros.launch_package} {profile.ros.launch_file} "
            + "> /tmp/tap_launch.log 2>&1 < /dev/null & echo launched"
        )
        remote.run(launch, record=False)
        log(f"[4/6] bringup 已用 {pty_path} 启动，采集 {args.seconds:.0f} 秒…")

        # --- 采集期间监控 tap 存活 ---
        # 两种死法都要发现：
        #   1. 进程消失 —— pgrep 能看到
        #   2. 进程还在但转发循环停摆 —— 只能靠 TAP_STATS 不再刷新发现
        # 第二种才是真正的坑：旧版 os.write 阻塞就是这个形态，
        # 当时 pgrep 一路正常，桥上却已经不通了。
        end = time.time() + args.seconds
        last_progress = None
        stalled = 0
        while time.time() < end:
            time.sleep(2)
            alive = remote.run("pgrep -f serial_tap.py > /dev/null; echo $?",
                               record=False).stdout.strip()
            if alive != "0":
                log("[!] 桥接进程已死亡，立即停止 bringup（安全兜底）")
                stop_launch(remote, profile)
                log(remote.read_file(TAP_OUT))
                return 2

            progress = _last_stats_progress(remote.read_file(TAP_OUT))
            if progress is None:
                continue          # 第一份 TAP_STATS 还没到，不算异常
            if progress == last_progress:
                stalled += 1
                if stalled >= STALL_LIMIT:
                    log(f"[!] 桥接进程还活着，但转发已停摆 "
                        f"（{stalled * 2} 秒无计数变化），立即停止 bringup")
                    stop_launch(remote, profile)
                    log(remote.read_file(TAP_OUT))
                    return 2
            else:
                stalled = 0
                last_progress = progress
        log("[5/6] 采集完成")

    except SSHError as exc:
        log(f"[!] SSH 错误：{exc}")
        return 1
    finally:
        # 无论成败，先把车停下
        try:
            stop_launch(remote, profile)
            kill_tap(remote)
        except Exception:
            pass

    # --- 取回并解析 ---
    try:
        def fetch(name: str) -> bytes:
            raw = remote.run(
                f"head -c 8000000 {tap_dir}/{name} 2>/dev/null | base64 -w0",
                timeout=120, record=False).stdout
            text = "".join((raw or "").split())
            return base64.b64decode(text) if text else b""

        down = fetch("from_mcu.raw")
        up = fetch("to_mcu.raw")
    finally:
        # 取回后清掉车上的日志目录。
        # 不留给下一次：第 20 项读的就是这个目录，残留数据会被当成
        # 下一次检测的结果（Round 10 审查）。必须放在 close() 之前。
        try:
            remote.run(f"rm -rf {tap_dir}", record=False)
        except Exception:
            pass
        remote.close()

    log("[6/6] 解析")
    log(f"  下行（下位机→上位机）{len(down)} 字节")
    log(f"  上行（上位机→下位机）{len(up)} 字节")

    if args.keep_raw:
        for name, data in (("from_mcu.raw", down), ("to_mcu.raw", up)):
            path = os.path.join(os.getcwd(), name)
            with open(path, "wb") as fh:
                fh.write(data)
            log(f"  已保存 {path}")

    down_stats, down_frames = analyze(down)
    up_stats, _ = analyze(up)

    if not down_frames:
        log("\n结论：下行没有任何可解析帧。")
        log(f"  丢弃字节 {down_stats.dropped_bytes}，长度错误 {down_stats.length_errors}")
        log("  最可能：波特率不对，或帧长公式与固件不一致。")
        return 3

    log(f"\n下行帧总数 {down_stats.frames}，bcc 错 {down_stats.bcc_errors}")
    log("  类型分布：")
    for name, count in sorted(down_stats.by_type.items()):
        log(f"    {name:<22} {count}")

    if up_stats.by_type:
        log("  上行类型分布：")
        for name, count in sorted(up_stats.by_type.items()):
            log(f"    {name:<22} {count}")

    # 电压专项判断
    if "Voltage_RESPONSE" in down_stats.by_type:
        volts = []
        for frame in down_frames:
            value = frame.voltage()
            if value is not None and 0 < value < 30:
                volts.append(value)
        if volts:
            log(f"\n电压：收到 {len(volts)} 个样本，"
                f"范围 {min(volts):.2f} ~ {max(volts):.2f} V")
    else:
        sent = "Voltage_RESPONSE" in up_stats.by_type
        log("\n电压：下位机全程未回传电压帧。")
        log(f"  上位机是否发过查询：{'是' if sent else '否 —— 这就是根因'}")

    return 0


def analyze(data: bytes):
    stats = ParseStats()
    parser = FrameParser(stats)
    frames = parser.feed(data)
    return stats, frames


if __name__ == "__main__":
    sys.exit(main())
