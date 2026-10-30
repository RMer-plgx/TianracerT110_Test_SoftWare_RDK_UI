#!/usr/bin/env python3
"""PyInstaller 打包脚本。

产物：dist/TianracerT110_Test_SoftWare_RDK.exe

使用：
    python build.py

注意：
- config/、report/、probes/ 会作为资源打进 exe，
  运行时通过 core/paths.py 的 resource_path() 定位（sys._MEIPASS）。
- test records/ 会创建在 exe 所在目录的上一级，不是解压目录。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "TianracerT110_Test_SoftWare_RDK"

# 需要一起打包的只读资源
DATA_DIRS = ["config", "report", "probes"]

# core/runner.py 用 __import__(f"checks.{name}") 动态导入，
# PyInstaller 的静态分析看不到它们。不显式列出，打包后每个检测项都会
# ModuleNotFoundError（审查发现）。
CHECK_MODULES = [
    "network", "identity", "rustdesk", "envvars", "devices", "hardware",
    "source", "lannet", "teb", "health", "traces", "imu", "lidar",
    "camera", "odom", "tf", "voltage", "mapping", "motion",
]

HIDDEN_IMPORTS = [
    "paramiko",
    "jinja2",
    "yaml",
] + [f"checks.{name}" for name in CHECK_MODULES]


def check_pyinstaller() -> bool:
    try:
        import PyInstaller  # noqa: F401
        return True
    except ImportError:
        return False


def main() -> int:
    print("=" * 60)
    print("  Tianracer T110 测试工具 — 打包")
    print("=" * 60)

    if not check_pyinstaller():
        print("\n缺少 PyInstaller。请先安装：")
        print("    pip install pyinstaller")
        return 1

    if sys.version_info < (3, 9):
        print(f"\nPython {sys.version_info.major}.{sys.version_info.minor} 版本过低")
        print("要求 3.9 及以上")
        return 1

    # 先跑单测，避免把坏代码打进去
    print("\n[1/3] 运行单元测试…")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"],
        cwd=ROOT,
    )
    if result.returncode != 0:
        print("\n❌ 单元测试未通过，已中止打包。")
        return 1
    print("✅ 测试通过")

    print("\n[2/3] 清理旧的构建产物…")
    for name in ("build", "dist"):
        target = ROOT / name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
    spec_file = ROOT / f"{APP_NAME}.spec"
    if spec_file.exists():
        spec_file.unlink()

    print("\n[3/3] 打包中（可能需要 1-3 分钟）…")
    separator = ";" if sys.platform == "win32" else ":"
    args = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--noconfirm",
        "--clean",
        "--name", APP_NAME,
        "--console",
    ]
    # 图标：只对 Windows 有意义，其他平台跳过
    icon_path = ROOT / "assets" / "app.ico"
    if sys.platform == "win32" and icon_path.exists():
        args += ["--icon", str(icon_path)]
        print(f"    使用图标：{icon_path.name}")
    elif sys.platform == "win32":
        print("    ⚠ 未找到 assets/app.ico，使用默认图标")
        print("      可用 python tools/make_icon.py 重新生成")

    for directory in DATA_DIRS:
        args += ["--add-data", f"{directory}{separator}{directory}"]
    for module in HIDDEN_IMPORTS:
        args += ["--hidden-import", module]
    args.append("run.py")

    result = subprocess.run(args, cwd=ROOT)
    if result.returncode != 0:
        print("\n❌ 打包失败")
        return 1

    exe = ROOT / "dist" / (f"{APP_NAME}.exe" if sys.platform == "win32" else APP_NAME)
    if not exe.exists():
        print(f"\n❌ 未找到产物：{exe}")
        return 1

    print("\n[4/4] 打包产物冒烟检查…")
    smoke = subprocess.run(
        [str(exe), "--help"], cwd=ROOT,
        capture_output=True, text=True, timeout=120,
        # Windows 默认用 GBK 解码子进程输出，exe 里含有非 GBK 字节就会
        # UnicodeDecodeError 崩在打包完成之后（实测踩过）。
        encoding="utf-8", errors="replace",
    )
    # --help 会打印用法并退出，不连车、不启动任何 ROS
    if smoke.returncode not in (0, 2):
        print(f"\n⚠ 冒烟检查返回码 {smoke.returncode}")
        print(f"  stderr: {smoke.stderr[:400]}")
        print("  打包产物可能不可用，请手动双击验证。")
    else:
        print("✅ 冒烟检查通过（--help 正常响应）")

    size_mb = exe.stat().st_size / (1024 * 1024)
    print("\n" + "=" * 60)
    print(f"  ✅ 打包完成：{exe}")
    print(f"     体积：{size_mb:.1f} MB")
    print("=" * 60)
    print("\n下一步：")
    print("  1. 双击 exe 试运行，确认能进入交互问答")
    print("  2. 按 RELEASE_GUIDE 上传到 GitHub Release")
    print("\n提示：PyInstaller 产物偶发被杀软误报，")
    print("      如遇拦截需在杀软中添加信任，或向用户说明。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
