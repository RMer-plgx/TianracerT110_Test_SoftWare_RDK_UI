"""2. 系统身份。"""

from __future__ import annotations

from core.verdict import CheckResult, Status


def run(ctx) -> CheckResult:
    result = CheckResult(number=2, title="系统身份", status=Status.PASS)
    r = ctx.remote

    info = r.run(
        "hostname; "
        "uname -r; "
        "grep -E '^PRETTY_NAME' /etc/os-release | cut -d= -f2- | tr -d '\"'; "
        "echo ---; "
        "source /opt/ros/noetic/setup.bash >/dev/null 2>&1; echo $ROS_DISTRO",
        timeout=20,
    )
    parts = [ln.strip() for ln in info.stdout.splitlines()]
    parts = [p for p in parts if p and p != "---"]

    hostname = parts[0] if len(parts) > 0 else "?"
    kernel = parts[1] if len(parts) > 1 else "?"
    os_name = parts[2] if len(parts) > 2 else "?"
    ros_distro = parts[3] if len(parts) > 3 else ""

    result.add("主机名", hostname)
    result.add("内核版本", kernel)
    result.add("操作系统", os_name, expected="Ubuntu 20.04 或 22.04")

    if ros_distro:
        result.add("ROS 版本", ros_distro, expected="noetic")
        if ros_distro != "noetic":
            result.status = Status.FAIL
            result.summary = f"ROS 版本为 {ros_distro}，应为 noetic"
    else:
        result.add("ROS 版本", "未检测到", note="/opt/ros/noetic 可能未安装")
        result.status = Status.WARN
        result.summary = "未检测到 ROS 版本"

    # IP 列表有助于确认是多网卡机器
    ips = r.run("hostname -I", timeout=15)
    result.add("本机地址", ips.stdout.strip() or "?")

    result.summary = result.summary or f"{hostname} / {os_name} / ROS {ros_distro}"
    return result
