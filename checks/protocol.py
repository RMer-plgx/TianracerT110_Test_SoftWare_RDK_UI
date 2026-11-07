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
两种方式，产物格式**完全一致**（两路纯原始字节流），解析端不区分：

- **首选：车端内嵌录音**（`docs/patches/`）。在底盘节点自己的收发路径上
  顺手写一份，通讯路径不变；录音模块挂掉只丢录音。需装车端补丁并重编
  `tianbot_core`。
- **备选：串口桥接**（`probes/serial_tap.py`）。不需改车端源码，代价是
  桥接成为控制链路单点：它挂掉链路就断，两边都不会立刻发现。

两者都把两个方向的原始字节分别落到 `to_mcu.raw` / `from_mcu.raw`。
本项把日志取回本地，用 `core/protocol.py` 离线解析。

未启用采集时本项判 SKIP，不判 FAIL —— 采集是可选增强，
不能因为没开就把车判成不可交付。
"""

from __future__ import annotations

import base64

from core.protocol import FrameParser, ParseStats, pack_type_name
from core.profile import TapConfig
from core.ssh import shquote
from core.verdict import CheckResult, Status

# 每方向最多回传这么多字节用于统计。2 MB 约 18 万帧，
# 足够算出稳定的错误率，又不会让 SSH 传输卡住。
MAX_FETCH_BYTES = 2 * 1024 * 1024

# bcc 错帧占比超过这个值就判线路有问题。
# 正常串口应该是 0；给 1% 容忍偶发噪声。
BCC_ERROR_LIMIT = 0.01


def _fetch(ctx, path: str):
    """把车上的二进制日志取回本地。

    走 base64 而不是直接 cat —— exec_command 的 stdout 是文本管道，
    原始二进制会被解码器改坏（0x0D/0x0A 翻译、编码替换）。

    返回 `(data, state)`，state 取值：
      - ``missing``   文件不存在 —— 真正的「未启用采集」
      - ``empty``     文件存在但长度为 0
      - ``ok``        取到了数据
      - ``error``     取回失败（SSH 超时 / base64 失败）

    必须区分这四种：取不回来当成「未启用」会把采集故障静默成 SKIP，
    而 SKIP 是不影响交付判定的 —— 等于把一次失败藏起来。

    存在性判断合成一条命令，省一次 SSH 往返（本项在 runtime 阶段末尾）。
    path 来自 profile 的 log_dir，只校验过「绝对路径」，未禁空格 ——
    一律引号包裹，否则带空格的路径会被 shell 拆成多个参数。
    """
    quoted = shquote(path)
    probe = ctx.remote.run(
        f"if [ -e {quoted} ]; then "
        f"if [ -s {quoted} ]; then echo nonempty; else echo empty; fi; "
        f"else echo missing; fi",
        timeout=20, record=False,
    ).stdout.strip()
    if probe == "missing":
        return b"", "missing"
    if probe == "empty":
        return b"", "empty"
    if probe != "nonempty":
        # 命令本身没跑成（SSH 不可达 / 超时）。这是传输层故障，
        # 不是「没启用采集」，不能静默成 SKIP。
        return b"", "error"

    cmd = f"head -c {MAX_FETCH_BYTES} {quoted} 2>/dev/null | base64 -w0"
    raw = ctx.remote.run(cmd, timeout=60, record=False).stdout
    text = "".join((raw or "").split())
    if not text:
        return b"", "error"
    try:
        return base64.b64decode(text), "ok"
    except Exception:
        return b"", "error"


def _tap_config(ctx) -> TapConfig:
    """取本产品的 tap 配置。拿不到就当成未启用。

    不抛异常：第 20 项是可选项，不能因为配置缺失把整个检测拖垮。
    但也不能猜 —— 拿到 None 一律视为未启用，宁可 SKIP 也不能伪 PASS。
    """
    profile = getattr(ctx, "profile", None)
    ros = getattr(profile, "ros", None) if profile is not None else None
    tap = getattr(ros, "tap", None) if ros is not None else None
    return tap if isinstance(tap, TapConfig) else TapConfig()


def _type_breakdown(stats: ParseStats) -> str:
    """把类型计数拼成一行短文本。

    不要直接把 dict 塞进 note —— 报告表格里会变成一整行大括号，
    而 note 是要在窄列里显示的。
    """
    if not stats.by_type:
        return ""
    items = sorted(stats.by_type.items(), key=lambda kv: -kv[1])
    text = "、".join(f"{name}×{count}" for name, count in items)
    return text if len(text) <= 80 else text[:77] + "…"


def run(ctx) -> CheckResult:
    result = CheckResult(number=20, title="通讯链路（协议层）", status=Status.PASS)

    # ---- 先看开关 ----
    # 第 20 项读的是车上的原始日志文件，而文件**存在不代表本次采过**：
    # tap_probe 只在采集开始前清目录，如果清理没做到位（手动跑过、
    # 中途异常退出），上一次的数据会一直留在那里。
    # 没有开关就无法区分「本次采集」与「上次残留」，会把旧数据当本次
    # 结果判 PASS —— 那是伪验证，比 SKIP 更危险。
    tap_cfg = _tap_config(ctx)
    if not tap_cfg.enabled:
        result.status = Status.SKIP
        result.summary = "本产品未启用底层采集，本项跳过"
        result.detail = (
            "未启用底层采集（本项为可选增强）。\n"
            "开关：profile.yaml 的 `ros.tap.enabled`，当前为 false。\n\n"
            "开启方式（首选）：车端内嵌录音。安装\n"
            "docs/patches/tianracer_core-serial-tap.patch，并在 profile 的\n"
            "env_overrides 里加 `TIANRACER_TAP_DIR: /tmp/tap`。\n"
            "它只在底盘节点的收发路径上顺手写一份，通信路径不变，\n"
            "录音坏了也不影响控制。\n\n"
            "备选：串口桥接（probes/serial_tap.py）。不用改车端源码，\n"
            "但桥接是控制链路单点故障，采集期间必须架空。\n\n"
            "未启用时其它检测项照常工作，判定不受影响。"
        )
        return result

    tap_dir = tap_cfg.log_dir
    to_mcu, up_state = _fetch(ctx, tap_cfg.log_path("to_mcu.raw"))
    from_mcu, down_state = _fetch(ctx, tap_cfg.log_path("from_mcu.raw"))

    # 传输故障不能当成「未启用」—— 那会把一次采集失败藏成 SKIP。
    if "error" in (up_state, down_state):
        result.status = Status.FAIL
        result.summary = "已采集但取回失败，无法判定链路"
        result.detail = (
            f"日志状态：上行 {up_state}、下行 {down_state}。\n"
            "文件在车上存在，但取回本地失败（SSH 超时或传输解压出错）。\n"
            "这是传输层问题，不代表协议链路异常，需重新采集。"
        )
        return result

    if not to_mcu and not from_mcu:
        result.status = Status.SKIP
        result.summary = "已启用采集但车上没有数据，本项跳过"
        result.detail = (
            f"开关已开（ros.tap.enabled=true），但 {tap_dir} 下没有可用日志。\n"
            "可能原因：采集未启动、启动后立即失败，或本次未跑桥接。\n"
            "未取得数据不能计为通过，需要重新采集后再判定。"
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

    result.add("下行帧数", str(total), note=_type_breakdown(down_stats))
    result.add("bcc 错误", f"{bad}", note=f"{ratio:.2%}")

    # ---- 上行（我们发给下位机的）用来看「有没有请求」 ----
    up_stats = ParseStats()
    up_parser = FrameParser(up_stats)
    up_parser.feed(to_mcu)

    # 上行空白但下行有数据：底盘节点可能根本没连上 pty，
    # 也可能用的是别的设备。其它项会报错，但单看第 20 项会误导。
    if not to_mcu:
        result.add("上行告警", "上位机未发出任何帧",
                   note="底盘节点可能未连到 pty，或设备路径不对")
    elif up_stats.frames == 0:
        result.add("上行告警", "上位机发出的字节无一帧可解析",
                   note="链路可能被旁路干扰，或波特率不一致")

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
    if not to_mcu or up_stats.frames == 0:
        result.summary += "；上行无可解析帧"
    return result
