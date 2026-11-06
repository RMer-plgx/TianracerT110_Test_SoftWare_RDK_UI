"""20. 通讯链路（协议层）。

这一项与其它所有检测项的根本区别：**它不读 ROS，读串口原始字节**。

存在的理由
----------
其余检测项全部依赖 ROS 话题。话题没数据时，报告只能说「没收到」，
无法区分四种成因：

    a) 下位机压根没发       → 硬件/固件问题
    b) 下位机发了但 ROS 丢了 → 驱动/节点问题
    c) 发了也收到了但解析错   → 软件问题
    d) 上位机从没请求过       → 协议使用问题

电压项长期属于 d) 的误判：源码里 `buildCmd` 只有 5 个调用点，没有一处
发起 `PACK_TYPE_Voltage_RESPONSE` 查询，于是推断「话题 advertise 了但
永不发布」。2026-11-06 实车采集推翻了后半句：下行 20 秒收到 3 帧
`Voltage_RESPONSE`（11.68~11.71 V），上行却没有任何电压查询 ——
说明下位机会**主动低频上报**，只是间隔极不规律。ROS 层短窗口订阅
抓不到，属于「发得太稀」，不是「从不发」。这类区分只有协议层能给出。

数据来源
--------
`probes/serial_tap.py` 在底盘节点与真实串口之间做 pty 桥接，
把两个方向的原始字节分别落到 `to_mcu.raw` / `from_mcu.raw`。
本项把日志取回本地，用 `core/protocol.py` 离线解析。

未启用采集时本项判 SKIP，不判 FAIL —— 采集是可选增强，
不能因为没开就把车判成不可交付。
"""

from __future__ import annotations

import base64

from core.protocol import FrameParser, ParseStats, pack_type_name
from core.verdict import CheckResult, Status

# 每方向最多回传这么多字节用于统计。2 MB 约 18 万帧，
# 足够算出稳定的错误率，又不会让 SSH 传输卡住。
MAX_FETCH_BYTES = 2 * 1024 * 1024

# bcc 错帧占比超过这个值就判线路有问题。
# 正常串口应该是 0；给 1% 容忍偶发噪声。
BCC_ERROR_LIMIT = 0.01


def _fetch(ctx, path: str) -> bytes:
    """把车上的二进制日志取回本地。

    走 base64 而不是直接 cat —— exec_command 的 stdout 是文本管道，
    原始二进制会被解码器改坏（0x0D/0x0A 翻译、编码替换）。
    """
    cmd = f"head -c {MAX_FETCH_BYTES} {path} 2>/dev/null | base64 -w0"
    raw = ctx.remote.run(cmd, timeout=60, record=False).stdout
    text = "".join((raw or "").split())
    if not text:
        return b""
    try:
        return base64.b64decode(text)
    except Exception:
        return b""


def run(ctx) -> CheckResult:
    result = CheckResult(number=20, title="通讯链路（协议层）", status=Status.PASS)

    tap_dir = getattr(ctx, "tap_dir", None) or "/tmp/tap"
    to_mcu = _fetch(ctx, f"{tap_dir}/to_mcu.raw")
    from_mcu = _fetch(ctx, f"{tap_dir}/from_mcu.raw")

    if not to_mcu and not from_mcu:
        result.status = Status.SKIP
        result.summary = "未启用底层采集，本项跳过"
        result.detail = (
            "开启方式：检测时启用串口桥接（probes/serial_tap.py）。\n"
            "未启用时其它检测项照常工作，判定不受影响。"
        )
        return result

    result.add("上行字节", str(len(to_mcu)), "B", note="上位机→下位机")
    result.add("下行字节", str(len(from_mcu)), "B", note="下位机→上位机")

    # ---- 下行（下位机回传）是判断链路是否活着的关键 ----
    down_stats = ParseStats()
    down_parser = FrameParser(down_stats)
    down_frames = down_parser.feed(from_mcu)

    if not down_frames:
        result.status = Status.FAIL
        result.summary = "下位机无任何可解析回传帧"
        result.detail = (
            f"已收到 {len(from_mcu)} 字节，但没有一帧通过帧头/长度校验。\n"
            "这说明波特率或帧格式不匹配，而不是「没有数据」。\n"
            f"丢弃字节 {down_stats.dropped_bytes}，长度错误 {down_stats.length_errors}。"
        )
        return result

    total = down_stats.frames
    bad = down_stats.bcc_errors
    ratio = bad / total if total else 0.0

    result.add("下行帧数", str(total), note=f"{down_stats.by_type}")
    result.add("bcc 错误", f"{bad}", note=f"{ratio:.2%}")

    # ---- 上行（我们发给下位机的）用来看「有没有请求」 ----
    up_stats = ParseStats()
    up_parser = FrameParser(up_stats)
    up_parser.feed(to_mcu)

    # ---- 电压帧专项 ----
    # 2026-11-06 实车确认：下位机会主动低频上报 Voltage_RESPONSE，
    # 上行并没有对应的查询帧。所以「上行没有查询」不能推出
    # 「不会有回传」—— 短窗口内没收到，只能说明采得太短。
    volt_frames = [f for f in down_frames if f.name == "Voltage_RESPONSE"]
    volts = [v for v in (f.voltage() for f in volt_frames)
             if v is not None and 0 < v < 30]
    if volts:
        result.add("电压回传", f"{min(volts):.2f}~{max(volts):.2f}", "V",
                   note=f"{len(volts)} 帧，下位机主动上报、间隔不规律")
    elif "Voltage_RESPONSE" in up_stats.by_type:
        result.add("电压诊断", "已发查询但本窗口内无回传",
                   note="加长采集窗口，或检查下位机固件")
    else:
        result.add("电压诊断", "本窗口内未捕到电压帧",
                   note="实测下位机会主动低频上报；窗口太短会漏采，不等于不回传")

    if ratio > BCC_ERROR_LIMIT:
        result.status = Status.FAIL
        result.summary = f"bcc 校验错误率 {ratio:.2%}，超过 {BCC_ERROR_LIMIT:.0%}"
        result.detail = (
            "高错误率通常意味着：波特率不匹配、线路干扰、\n"
            "或帧长公式与固件不一致（见 core/protocol.py 的 LEN_TO_TOTAL）。"
        )
        return result

    if down_stats.length_errors:
        result.add("长度错误帧", str(down_stats.length_errors),
                   note="已丢弃并重新同步")

    known = {k: v for k, v in down_stats.by_type.items()
             if not k.startswith("UNKNOWN_")}
    result.summary = (f"链路正常，下行 {total} 帧 / {len(known)} 种类型"
                      f"，bcc 错误 {bad}")
    return result
