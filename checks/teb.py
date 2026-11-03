"""16. teb 导航插件。

只验证插件是否注册、地图文件是否存在，不启动导航（那需要 move_base 跑起来且车会动）。
"""

from __future__ import annotations

from core.verdict import CheckResult, Status

from ._util import as_list, parse_float


def run(ctx) -> CheckResult:
    result = CheckResult(number=16, title="teb 导航插件", status=Status.PASS)
    r = ctx.remote

    found = r.run(
        "source /opt/ros/noetic/setup.bash >/dev/null 2>&1; "
        "rospack find teb_local_planner 2>&1",
        timeout=25,
    )
    path = found.stdout.strip()
    result.add("teb_local_planner 路径", path or "未找到")
    if not path or "not found" in path.lower() or "ERROR" in path:
        result.status = Status.FAIL
        result.summary = "teb_local_planner 未安装，导航无法使用"
        result.detail = "修复：sudo apt install -y ros-noetic-teb-local-planner"
        return result

    so = r.run("ls /opt/ros/noetic/lib/libteb_local_planner.so 2>&1", timeout=15)
    has_so = ".so" in so.stdout and "No such" not in so.stdout
    result.add("插件库文件", "存在" if has_so else "缺失")

    plugins = r.run(
        "source /opt/ros/noetic/setup.bash >/dev/null 2>&1; "
        "rospack plugins --attrib=plugin nav_core 2>&1 | grep -i teb",
        timeout=25,
    )
    registered = "teb" in plugins.stdout.lower()
    result.add("nav_core 注册", "已注册" if registered else "未注册")

    # 地图文件
    maps = r.run(
        f"ls -1 {ctx.profile.ros.workspace}/{ctx.profile.ros.maps_dir}/*.yaml 2>&1",
        timeout=15,
    )
    yaml_files = [p for p in as_list(maps.stdout) if p.endswith(".yaml")]
    result.add("可用地图", len(yaml_files), "个", expected="≥ 1")
    if yaml_files:
        result.add("地图文件", yaml_files[0].split("/")[-1])

        resolution = r.run(f"grep '^resolution' {yaml_files[0]}", timeout=15)
        value = parse_float(resolution.stdout)
        if value is not None:
            expected_res = ctx.baseline.get("mapping", {}).get("expected_resolution", 0.05)
            result.add("地图分辨率", f"{value:.3f}", "m/pix",
                       expected=f"{expected_res}")

    if not has_so or not registered:
        result.status = Status.WARN
        result.summary = "插件路径存在但库文件或注册信息不完整"
    elif not yaml_files:
        result.status = Status.WARN
        result.summary = "插件正常，但没有可用地图文件，导航无法启动"
    else:
        result.summary = f"插件已注册，{len(yaml_files)} 个地图可用"
    return result
