#!/usr/bin/env python3
"""生成 README 用的界面截图（深色 + 浅色）。

用离屏模式渲染，不需要真实显示窗口，也不需要连车。
改完主题后重新跑一次即可更新文档配图。
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs"


def main() -> int:
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)

    from ui.main_window import MainWindow

    OUT.mkdir(exist_ok=True)
    window = MainWindow()
    window.resize(1280, 820)
    window.show()
    app.processEvents()

    # 默认深色
    window.grab().save(str(OUT / "screenshot_dark.png"))
    print(f"已保存 {OUT / 'screenshot_dark.png'}")

    window.toggle_theme()
    app.processEvents()
    window.grab().save(str(OUT / "screenshot_light.png"))
    print(f"已保存 {OUT / 'screenshot_light.png'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
