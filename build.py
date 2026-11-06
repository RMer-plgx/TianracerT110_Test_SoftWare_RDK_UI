#!/usr/bin/env python3
"""PyInstaller 打包脚本（图形界面版）。

产物：dist/tb-verify-ui.exe

注意：
- products/、report/、probes/、assets/ 会作为资源打进 exe
- test records/ 创建在 exe 所在目录
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "tb-verify-ui"

DATA_DIRS = ["products", "report", "probes", "assets"]

# core/runner.py 用 __import__(f"checks.{name}") 动态导入，
# PyInstaller 静态分析看不到，必须显式列出，否则打包后每项都报错。
CHECK_MODULES = [
    "network", "identity", "rustdesk", "envvars", "devices", "hardware",
    "source", "lannet", "teb", "health", "traces", "imu", "lidar",
    "camera", "odom", "tf", "voltage", "mapping", "protocol", "motion",
]

HIDDEN_IMPORTS = [
    "paramiko",
    "jinja2",
    "yaml",
    "PyQt5.QtCore",
    "PyQt5.QtGui",
    "PyQt5.QtWidgets",
    "ui.main_window",
    "ui.cards",
    "ui.theme",
] + [f"checks.{name}" for name in CHECK_MODULES]

# PyQt5 里用不到的 Qt 模块，排除后能显著减小体积
EXCLUDES = [
    "PyQt5.QtWebEngineWidgets",
    "PyQt5.QtWebEngineCore",
    "PyQt5.QtMultimedia",
    "PyQt5.QtQml",
    "PyQt5.QtQuick",
    "PyQt5.Qt3DCore",
    "PyQt5.QtBluetooth",
    "PyQt5.QtNfc",
    "PyQt5.QtPositioning",
    "PyQt5.QtSerialPort",
    "PyQt5.QtTest",
    "PyQt5.QtSql",
    "PyQt5.QtDesigner",
    "PyQt5.QtHelp",
    "tkinter",
    "matplotlib",
    "numpy",
    "pandas",
]


def main() -> int:
    print("=" * 60)
    print("  Tianracer T110 整车测试（界面版）— 打包")
    print("=" * 60)

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("\n缺少 PyInstaller。请先安装：")
        print("    pip install pyinstaller")
        return 1

    try:
        import PyQt5  # noqa: F401
    except ImportError:
        print("\n缺少 PyQt5。请先安装：")
        print("    pip install PyQt5")
        return 1

    if sys.version_info < (3, 9):
        print(f"\nPython {sys.version_info.major}.{sys.version_info.minor} 过低，要求 3.9+")
        return 1

    print("\n[1/4] 运行单元测试…")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"], cwd=ROOT
    )
    if result.returncode != 0:
        print("\n❌ 单元测试未通过，已中止打包。")
        return 1
    print("✅ 测试通过")

    print("\n[2/4] 清理旧的构建产物…")
    target = ROOT / "build" / "ui"
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    spec_file = ROOT / f"{APP_NAME}.spec"
    if spec_file.exists():
        spec_file.unlink()

    print("\n[3/4] 打包中（可能需要 2-5 分钟）…")
    separator = ";" if sys.platform == "win32" else ":"
    args = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--noconfirm",
        "--clean",
        "--windowed",              # GUI 程序，不显示控制台
        "--name", APP_NAME,
        "--workpath", str(ROOT / "build" / "ui"),
    ]

    icon_path = ROOT / "assets" / "app.ico"
    if sys.platform == "win32" and icon_path.exists():
        args += ["--icon", str(icon_path)]
        print(f"    使用图标：{icon_path.name}")

    for directory in DATA_DIRS:
        args += ["--add-data", f"{directory}{separator}{directory}"]
    for module in HIDDEN_IMPORTS:
        args += ["--hidden-import", module]
    for module in EXCLUDES:
        args += ["--exclude-module", module]

    args.append("app.py")

    result = subprocess.run(args, cwd=ROOT)
    if result.returncode != 0:
        print("\n❌ 打包失败")
        return 1

    exe = ROOT / "dist" / (f"{APP_NAME}.exe" if sys.platform == "win32" else APP_NAME)
    if not exe.exists():
        print(f"\n❌ 未找到产物：{exe}")
        return 1

    size_mb = exe.stat().st_size / (1024 * 1024)
    print("\n[4/4] 打包产物自检…")
    # GUI 程序不能用 --help 做皮试，必须靠 --self-check：
    # 检查资源、全部 checks 模块、报告模板能否真正工作。
    smoke = subprocess.run(
        [str(exe), "--self-check"], cwd=ROOT,
        capture_output=True, text=True, timeout=180,
        encoding="utf-8", errors="replace",
    )
    if smoke.returncode != 0:
        print("  自检未通过：")
        for line in (smoke.stdout or "").splitlines()[-15:]:
            print(f"    {line}")
        print("  打包产物可能不可用，请手动双击验证。")
        return 1
    else:
        print(f"  ✅ 自检通过（资源 + {len(CHECK_MODULES)} 个 checks + 报告模板均正常）")

    print("\n" + "=" * 60)
    print(f"  ✅ 打包完成：{exe}")
    print(f"     体积：{size_mb:.1f} MB")
    print("=" * 60)
    print("\n下一步：")
    print("  1. 双击 exe，确认界面能出来（不需要装 Python）")
    print("  2. 用不通的 IP 走一遍流程，确认报告生成在 exe 同目录")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
