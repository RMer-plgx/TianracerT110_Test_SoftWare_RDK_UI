"""6. 主控平台与序列号。

用户 2026-11-13 的要求原话：

> 确认是 RDK 平台，能读到序列号就 pass，如果不是 RDK X5 或者读不到
> 序列号就报错结束。

## 为什么不再读 CP2102 的序列号

旧实现跑的是：

    udevadm info -q property -n /dev/ttyUSB0 | grep ID_SERIAL_SHORT

`/dev/ttyUSB0` 是 **CP2102 USB 转串口芯片**，不是主控。CP2102 出厂时
厂家可以不烧唯一 ID，不烧就是默认 `0001` —— 同一批模块全一样。
所以那个字段无法识别车辆，比对它只会制造假告警（全批 WARN）。

主控是 **D-Robotics RDK X5**（不是 Jetson），它的序列号在
`/proc/device-tree/serial-number`，是硬件级的、每台唯一的。

实测（2026-11-13）：

    192.168.1.82  model=D-Robotics X5 MD V0.2  serial=308118a90a2dc19403bb44d80000024
    192.168.1.93  model=D-Robotics X5 MD V1.2  serial=328150681214c98a00d868f80012004

## 为什么这一项是 fatal

平台不对，后面所有阈值（传感器频率、电压范围、相机分辨率、轴距轮距）
都可能是错的。继续测会给出误导性的 PASS，比直接停下更糟。
"""

from __future__ import annotations

from core.verdict import CheckResult, Status

from ._util import parse_key_value

# 读 device-tree 的辅助：`tr -d '\0'` 去掉字符串尾部的 NUL，
# 否则比较时会带上不可见字符。
_READ_DT = (
    "cat /proc/device-tree/{name} 2>/dev/null | tr -d '\\0'"
)


def _read_dt(r, name: str) -> str:
    return r.run(_READ_DT.format(name=name), timeout=15, record=False).stdout.strip()


def run(ctx) -> CheckResult:
    result = CheckResult(number=6, title="主控平台与序列号", status=Status.PASS)
    baseline = ctx.baseline.get("hardware", {})
    r = ctx.remote

    # ---- 平台确认 ----
    model = _read_dt(r, "model")
    compatible = _read_dt(r, "compatible")
    serial = _read_dt(r, "serial-number")

    result.add("平台型号", model or "读取失败")
    if compatible:
        # compatible 可能是多个 NUL 分隔的串，转成逗号分隔便于阅读
        result.add("compatible", compatible.replace("\x00", ", "))

    keywords = baseline.get("required_platform_keywords") or [
        "D-Robotics X5", "D-Robotics, x5",
    ]
    probe = f"{model} {compatible}"
    matched = next((k for k in keywords if k in probe), "")

    if not matched:
        result.status = Status.FAIL
        result.summary = (
            f"不是 RDK X5 平台（读到 model={model or '空'}），"
            f"后续检测项的阈值不适用，已中止"
        )
        result.detail = baseline.get("note", "")
        return result

    # ---- 序列号 ----
    result.add("主控序列号", serial or "读取失败")

    if not serial:
        result.status = Status.FAIL
        result.summary = "平台正确但读不到序列号（device-tree 无 serial-number），已中止"
        result.detail = (
            "device-tree 里应该有 /proc/device-tree/serial-number。\n"
            "读不到说明镜像或设备树不完整，车辆身份无法确认。\n"
            + baseline.get("note", "")
        )
        return result

    # ---- CP2102 只作拓扑记录，不判序列号 ----
    vendor = baseline.get("vendor_id", "10c4")
    model_id = baseline.get("model_id", "ea60")
    props = r.run(
        "udevadm info -q property -n /dev/ttyUSB0 2>&1 | "
        "grep -E 'ID_VENDOR_ID|ID_MODEL_ID'",
        timeout=20,
    )
    parsed = parse_key_value(props.stdout)
    usb_vendor = parsed.get("ID_VENDOR_ID")
    usb_model = parsed.get("ID_MODEL_ID")
    result.add("底盘串口芯片", f"{usb_vendor or '?'}:{usb_model or '?'}",
               expected=f"{vendor}:{model_id}",
               note="CP2102 USB 转串口；只验证拓扑，不比对它的序列号"
                    "（同批模块的 ID_SERIAL_SHORT 全是 0001，无区分度）")

    if usb_vendor and usb_vendor != vendor:
        result.status = Status.WARN
        result.summary = (
            f"{model}，序列号 {serial}；但底盘串口芯片是 "
            f"{usb_vendor}:{usb_model}，不是预期的 CP2102"
        )
        return result

    result.summary = f"{model}，序列号 {serial}"
    return result
