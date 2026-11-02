#!/usr/bin/env python3
"""Tianracer T110 整车测试 — 图形界面版。

用法：
    python app.py                正常启动界面
    python app.py --self-check   不建窗口，检查资源与依赖是否齐全（打包验证用）

所有检测逻辑复用本工程内的 core/ 与 checks/（独立一份，
与命令行版互不影响）。
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

# paramiko 依赖的 cryptography 会自报弃用警告，与本项目无关但会刷屏。
# 只按 message 精准屏蔽，不要一刀切 category=DeprecationWarning——
# 那会把本项目自己将来引入的废弃 API 也吞掉。
warnings.filterwarnings("ignore", message=".*CryptographyDeprecationWarning.*")
warnings.filterwarnings("ignore", message=".*TripleDES.*")
warnings.filterwarnings("ignore", message=".*Blowfish.*")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.paths import ensure_utf8_env, records_root, resource_path  # noqa: E402

CHECK_MODULES = [
    "network", "identity", "rustdesk", "envvars", "devices", "hardware",
    "source", "lannet", "teb", "health", "traces", "imu", "lidar",
    "camera", "odom", "tf", "voltage", "mapping", "motion",
]


def self_check() -> int:
    """不建窗口的自检，专门用于验证打包产物。

    检查四项：
      1. 资源文件是否打进包（config / report / probes / assets）
      2. 19 个 checks 模块能否导入（动态导入，PyInstaller 容易漏）
      3. 判定与报告模板能否正常工作
      4. 报告目录能否确定（不要求可写，只检查路径解析）
    """
    print("self-check 开始")
    problems = []

    for rel in ("config/baseline.yaml", "report/md.j2", "report/html.j2",
                "assets/app.ico", "probes/motion_probe.py"):
        path = resource_path(rel)
        status = "OK" if path.exists() else "MISSING"
        print(f"  resource {rel}: {status}")
        if not path.exists():
            problems.append(f"资源缺失: {rel}")

    import importlib
    for name in CHECK_MODULES:
        try:
            importlib.import_module(f"checks.{name}")
        except Exception as exc:
            print(f"  checks.{name}: FAIL {type(exc).__name__}: {exc}")
            problems.append(f"checks.{name} 导入失败")
    print(f"  checks 模块: {len(CHECK_MODULES) - len(problems)}/{len(CHECK_MODULES)} 可导入")

    # 判定与模板
    try:
        from core.reporting import render
        from core.verdict import CheckResult, Report, Status
        import tempfile

        report = Report(host="0.0.0.0", user="self-check", started_at="now")
        report.add(CheckResult(number=1, title="自检", status=Status.PASS,
                               summary="打包产物可用"))
        with tempfile.TemporaryDirectory() as tmp:
            render(report, Path(tmp))
        print("  报告渲染: OK")
    except Exception as exc:
        print(f"  报告渲染: FAIL {type(exc).__name__}: {exc}")
        problems.append(f"报告渲染失败: {exc}")

    try:
        root = records_root()
        print(f"  报告目录: {root}")
    except Exception as exc:
        problems.append(f"报告目录解析失败: {exc}")

    # PyQt5 能否加载（不建窗口）
    try:
        from PyQt5.QtWidgets import QApplication  # noqa: F401
        print("  PyQt5: OK")
    except Exception as exc:
        print(f"  PyQt5: FAIL {type(exc).__name__}: {exc}")
        problems.append(f"PyQt5 导入失败: {exc}")

    # PDF 导出依赖 QWebEngine。必须在创建 QApplication **之前**导入，
    # 否则 Qt 会拒绝（Qt WebEngine 初始化时会校验这一约束）。
    try:
        from PyQt5 import QtWebEngineWidgets  # noqa: F401
        print("  PDF 导出内核 (QWebEngine): OK")
    except Exception as exc:
        print(f"  PDF 导出内核 (QWebEngine): 不可用 {type(exc).__name__}: {exc}")
        print("    → PDF 按钮会降级为「用浏览器 Ctrl+P」，其余功能不受影响")

    # 屏幕信息：窗口尺寸自适应靠它，出问题时要能查
    try:
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QApplication
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        # 导入了 QtWebEngine 就必须设这个，否则创建 QApplication 直接 abort
        QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
        app = QApplication.instance() or QApplication([])
        screen = app.primaryScreen()
        avail = screen.availableGeometry()
        print(f"  屏幕: 物理 {screen.size().width()}x{screen.size().height()}"
              f" | 可用 {avail.width()}x{avail.height()}"
              f" | DPR {screen.devicePixelRatio():.2f}"
              f" | 逻辑DPI {screen.logicalDotsPerInch():.0f}")
    except Exception as exc:
        print(f"  屏幕信息: 无法获取 {type(exc).__name__}: {exc}")

    if problems:
        print(f"\nself-check 失败（{len(problems)} 项）")
        for item in problems:
            print(f"  - {item}")
        return 1

    print("\nself-check 通过")
    return 0


def diagnose() -> int:
    """诊断用：用真实图形平台初始化，报告屏幕与窗口尺寸。

    专门用于排查「打包后窗口被压变小」这类环境差异问题——
    --self-check 用离屏平台，屏幕信息是假的。

    打包后是 --windowed 没有控制台，所以结果同时写到 exe 旁的 diag.txt。
    """
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    app = QApplication.instance() or QApplication(sys.argv)

    screen = app.primaryScreen()
    avail = screen.availableGeometry()
    lines = [
        f"平台: {app.platformName()}",
        f"物理: {screen.size().width()}x{screen.size().height()}",
        f"可用: {avail.width()}x{avail.height()}",
        f"DPR : {screen.devicePixelRatio():.2f}",
        f"DPI : {screen.logicalDotsPerInch():.0f}",
    ]

    from ui.main_window import MainWindow
    window = MainWindow()
    window.show()
    app.processEvents()
    lines.append(f"窗口: {window.width()}x{window.height()}")
    lines.append(f"最小: {window.minimumWidth()}x{window.minimumHeight()}")
    window.close()

    text = "\n".join(lines)
    print(text)
    try:
        from core.paths import app_dir
        (app_dir() / "diag.txt").write_text(text, encoding="utf-8")
    except Exception:
        pass
    return 0


def pdf_smoke(out_path: str) -> int:
    """真跑一次 HTML → PDF，验证打包后的 QWebEngine 可用。

    为什么要这个：--self-check 只证明「QWebEngineWidgets 能 import」，
    证明不了 QtWebEngineProcess.exe 在 onefile 解压目录里能被拉起来。
    后者才是打包版真正会翻车的地方（资源路径 / UPX / 子进程路径）。
    """
    from PyQt5.QtCore import Qt, QTimer, QUrl
    from PyQt5.QtWidgets import QApplication

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    app = QApplication([])

    from PyQt5.QtWebEngineWidgets import QWebEngineView

    # 用真实报告模板渲染一份，顺带验证模板本身
    import tempfile
    from core.reporting import render
    from core.verdict import CheckResult, Report, Status

    report = Report(host="0.0.0.0", user="pdf-smoke",
                    started_at="2026-01-01 00:00:00")
    report.add(CheckResult(number=1, title="自检", status=Status.PASS,
                           summary="PDF 导出链路自检"))

    tmpdir = tempfile.mkdtemp(prefix="ttr-pdf-")
    html_path, _ = render(report, Path(tmpdir))
    html_path = html_path.parent / "report.html"

    out = Path(out_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    state = {"ok": False}

    view = QWebEngineView()
    view.resize(1240, 1754)
    page = view.page()

    def on_finished(path: str, ok: bool) -> None:
        state["ok"] = ok
        app.quit()

    page.pdfPrintingFinished.connect(on_finished)

    def on_load(ok: bool) -> None:
        if not ok:
            print("页面加载失败")
            app.quit()
            return
        page.printToPdf(str(out))

    view.loadFinished.connect(on_load)
    view.load(QUrl.fromLocalFile(str(html_path)))
    QTimer.singleShot(60000, app.quit)   # 卡死也要有出口
    app.exec_()

    if state["ok"] and out.exists() and out.stat().st_size > 1000:
        print(f"PDF 生成成功：{out}（{out.stat().st_size} 字节）")
        return 0
    print(f"PDF 生成失败（ok={state['ok']}, exists={out.exists()}）")
    return 1


def main() -> int:
    ensure_utf8_env()

    if "--self-check" in sys.argv:
        return self_check()
    if "--diag" in sys.argv:
        return diagnose()
    if "--pdf-smoke" in sys.argv:
        index = sys.argv.index("--pdf-smoke")
        target = (sys.argv[index + 1] if len(sys.argv) > index + 1
                  else "pdf_smoke.pdf")
        return pdf_smoke(target)

    # 高分屏缩放：必须在 QApplication 创建前设置
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QFont
    from PyQt5.QtWidgets import QApplication, QMessageBox

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    # QWebEngine 生成 PDF 需要共享 OpenGL 上下文，且必须在 QApplication 之前设置。
    # 不设的话创建 QWebEngineView 会直接崩或黑屏。
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

    app = QApplication(sys.argv)
    app.setApplicationName("Tianracer T110 整车测试")
    app.setFont(QFont("Microsoft YaHei UI", 9))

    try:
        from ui.main_window import MainWindow
        window = MainWindow()
        window.show()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        QMessageBox.critical(
            None, "启动失败",
            f"界面初始化失败：\n\n{type(exc).__name__}: {exc}\n\n"
            f"程序目录：{Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path.cwd()}",
        )
        return 1

    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
