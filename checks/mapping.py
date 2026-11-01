"""15. 激光建图链路。

只验证 gmapping 能否起来并产出地图元数据，不验证地图质量
（那需要车移动绕场，属人工项）。

关键：必须先有 bringup（提供 odom→base_footprint），
单独启 lidar.launch 会让 gmapping 报 Dropped 100%。
"""

from __future__ import annotations

import time

from core.remote import kill_all, ros, start_launch
from core.verdict import CheckResult, Status

from ._util import parse_float


def run(ctx) -> CheckResult:
    result = CheckResult(number=15, title="激光建图链路", status=Status.PASS)
    cfg = ctx.baseline.get("mapping", {})
    r = ctx.remote

    log_path = "/tmp/tt_slam.log"
    start_launch(r, "tianracer_slam", "tianracer_gmapping.launch",
                 log_path=log_path)
    time.sleep(12)

    nodes = r.run(ros("rosnode list 2>/dev/null"), timeout=20).stdout
    has_slam = "slam_gmapping" in nodes
    result.add("gmapping 节点", "已启动" if has_slam else "未出现")

    if not has_slam:
        result.status = Status.FAIL
        result.summary = "gmapping 未能启动"
        tail = r.run(f"tail -15 {log_path} 2>&1", timeout=15).stdout
        result.detail = tail.strip()
        _cleanup(ctx, log_path)
        return result

    info = r.run(
        ros("timeout 20 rostopic echo -n 1 /tianracer/map/info 2>&1"),
        timeout=35,
    )
    resolution = _grab(info.stdout, "resolution")
    width = _grab(info.stdout, "width")
    height = _grab(info.stdout, "height")

    exp_res = float(cfg.get("expected_resolution", 0.05))
    exp_size = int(cfg.get("expected_map_size", 1984))

    if resolution is None:
        result.status = Status.FAIL
        result.summary = "gmapping 已启动但未产出 /tianracer/map（可能拍丢了 odom TF）"
        warn = r.run(f"grep -c Dropped {log_path} 2>&1", timeout=15).stdout.strip()
        if warn and warn != "0":
            result.detail = ("日志出现 Dropped 告警，说明 gmapping 收不到 odom。\n"
                             "确认 bringup 已启动（提供 odom→base_footprint），"
                             "而不是只启了 lidar.launch。")
        _cleanup(ctx, log_path)
        return result

    result.add("分辨率", f"{resolution:.4f}", "m/pix", expected=f"{exp_res}")
    result.add("地图尺寸", f"{int(width)}×{int(height)}" if width and height else "?",
               expected=f"{exp_size}×{exp_size}")

    res_ok = abs(resolution - exp_res) < 1e-6
    size_ok = (width is not None and height is not None
               and int(width) == exp_size and int(height) == exp_size)

    if res_ok and size_ok:
        result.summary = f"链路正常，地图 {int(width)}×{int(height)} @ {resolution:g} m/pix"
    else:
        result.status = Status.WARN
        result.summary = "地图参数与基线不同"

    result.detail = "地图实际质量（扩展、闭环、重影）需人工遥控走场验证。"
    _cleanup(ctx, log_path)
    return result


def _grab(text: str, key: str):
    for line in (text or "").splitlines():
        if line.strip().startswith(key + ":"):
            return parse_float(line.split(":", 1)[1])
    return None


def _cleanup(ctx, log_path: str) -> None:
    """停掉本项启动的 gmapping，避免影响后续运动测试。"""
    ctx.remote.run("pkill -INT -f 'roslaunch tianracer_slam' || true",
                   record=False)
    time.sleep(3)
    ctx.remote.run("pkill -9 -f slam_gmapping || true", record=False)
