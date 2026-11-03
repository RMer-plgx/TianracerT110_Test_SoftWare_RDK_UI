"""5. 设备节点。

本车期望的节点与常见缺失项都在这里核对。
注意 /dev/tianbot_lidar -> ttyUSB0 是已知的错误软链指向，报告里说明但不修改。
"""

from __future__ import annotations

from core.verdict import CheckResult, Status


REQUIRED = {
    "/dev/ttyUSB0": "底盘主控（CP2102）",
    "/dev/tianbot_usb_cam": "相机软链",
    "/dev/video0": "摄像头设备",
}

OPTIONAL = {
    "/dev/ttyACM0": "ESP32 工具板（未插属正常）",
    "/dev/tianbot_toolkit": "工具板软链",
    "/dev/tianbot_base": "底盘软链（udev 规则缺陷，本车不期望存在）",
}

SUSPICIOUS = {
    "/dev/tianbot_lidar": (
        "本车雷达走网口（192.168.198.2），不需要串口。"
        "此软链若指向 ttyUSB0（底盘）是错误配置，正是它可能让 oradar 分支抢占底盘串口"
    ),
}


def run(ctx) -> CheckResult:
    result = CheckResult(number=5, title="设备节点", status=Status.PASS)
    r = ctx.remote

    present = set()
    listing = r.run("ls -l /dev/ttyUSB* /dev/ttyACM* /dev/video* 2>&1", timeout=20)
    result.add_command("ls -l /dev/ttyUSB* /dev/ttyACM* /dev/video*")

    for path in list(REQUIRED) + list(OPTIONAL) + list(SUSPICIOUS):
        exists = r.run(f"test -e {path}", timeout=10, record=False).ok
        if exists:
            present.add(path)

    missing_required = []
    for path, desc in REQUIRED.items():
        if path in present:
            result.add(path, "存在", note=desc)
        else:
            missing_required.append(path)
            result.add(path, "缺失", expected="存在", note=desc)

    for path, desc in OPTIONAL.items():
        result.add(path, "存在" if path in present else "缺失", note=desc)

    # 软链指向检查
    warnings = []
    for path, why in SUSPICIOUS.items():
        if path not in present:
            continue
        target = r.run(f"readlink -f {path}", timeout=10, record=False).stdout.strip()
        result.add(path, f"→ {target}" if target else "存在", note="可疑软链")
        if "ttyUSB0" in target:
            warnings.append(path)
            result.add("软链冲突", f"{path} 指向底盘串口", note=why)

    if missing_required:
        result.status = Status.WARN
        result.summary = "缺失关键节点：" + "、".join(missing_required)
        if "/dev/ttyUSB0" in missing_required:
            result.status = Status.FAIL
            result.summary = "底盘串口 /dev/ttyUSB0 缺失，无法继续"
    elif warnings:
        # 软链重复指向不影响底盘通信，但属于交付前该清理的卫生问题。
        # 如实报 WARN，是否接受由人在报告里逐条确认。
        result.status = Status.WARN
        result.summary = f"{len(warnings)} 个软链指向可疑，建议清理（不影响本次测试）"
    else:
        result.summary = f"必需节点齐全（共检测 {len(REQUIRED) + len(OPTIONAL) + len(SUSPICIOUS)} 项）"

    if listing.stdout.strip():
        result.detail = listing.stdout.strip()
    return result
