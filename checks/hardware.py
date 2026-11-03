"""6. 主控硬件 ID。

已知偏差：序列号 0001（文档要求 1001）。判 WARN 不判 FAIL，
因为底盘通信与里程计都正常，只是版本较旧。
"""

from __future__ import annotations

from core.verdict import CheckResult, Status

from ._util import parse_key_value


def run(ctx) -> CheckResult:
    result = CheckResult(number=6, title="主控硬件 ID", status=Status.PASS)
    baseline = ctx.baseline.get("hardware", {})
    r = ctx.remote

    props = r.run(
        "udevadm info -q property -n /dev/ttyUSB0 2>&1 | "
        "grep -E 'ID_VENDOR_ID|ID_MODEL_ID|ID_MODEL=|ID_SERIAL_SHORT'",
        timeout=20,
    )
    parsed = parse_key_value(props.stdout)

    vendor = parsed.get("ID_VENDOR_ID")
    model_id = parsed.get("ID_MODEL_ID")
    model = parsed.get("ID_MODEL")
    serial = parsed.get("ID_SERIAL_SHORT")

    expect_vendor = baseline.get("vendor_id", "10c4")
    expect_model_id = baseline.get("model_id", "ea60")
    expect_serial = str(baseline.get("expected_serial", "1001"))
    acceptable = [str(s) for s in baseline.get("acceptable_serials", [])]

    result.add("ID_VENDOR_ID", vendor or "读取失败", expected=expect_vendor)
    result.add("ID_MODEL_ID", model_id or "读取失败", expected=expect_model_id)
    result.add("ID_MODEL", model or "读取失败")
    result.add("ID_SERIAL_SHORT", serial or "读取失败", expected=expect_serial)

    problems = []
    if vendor != expect_vendor:
        problems.append(f"vendor {vendor} ≠ {expect_vendor}")
    if model_id != expect_model_id:
        problems.append(f"model {model_id} ≠ {expect_model_id}")

    if problems:
        result.status = Status.FAIL
        result.summary = "硬件 ID 不符：" + "；".join(problems)
        return result

    if serial == expect_serial:
        result.summary = f"CP2102 序列号 {serial}，符合文档要求"
    elif serial in acceptable:
        # 通信正常，但版本比文档旧。BRIEF 明确要求「判 WARN 不判 FAIL」，
        # 是否接受由人与开发确认后在报告里勾选。
        result.status = Status.WARN
        result.summary = (f"序列号 {serial} 为旧版主控（文档要求 {expect_serial}），"
                          f"通信正常但需与开发确认版本可接受性")
        result.detail = baseline.get("note", "")
    else:
        result.status = Status.WARN
        result.summary = f"序列号 {serial} 不在预期范围（文档 {expect_serial}）"

    return result
