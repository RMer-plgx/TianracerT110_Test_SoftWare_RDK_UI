"""单个检测项的状态卡片。

设计要点：
- 颜色只由状态决定，不在卡片内部写死，方便换主题
- 状态变化时不销毁重建，只改样式，避免滚动位置跳动
"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from core.verdict import Status

from .theme import palette

# 状态 → 左侧色条颜色 key
STATUS_COLOR_KEY = {
    Status.PASS: "pass",
    Status.WARN: "warn",
    Status.FAIL: "fail",
    Status.SKIP: "skip",
    Status.MANUAL: "skip",
    Status.PENDING: "pending",
}

STATUS_TEXT = {
    Status.PASS: "通过",
    Status.WARN: "告警",
    Status.FAIL: "失败",
    Status.SKIP: "跳过",
    Status.MANUAL: "人工",
    Status.PENDING: "待验证",
}


class CheckCard(QFrame):
    """19 项里的一项。"""

    def __init__(self, number: int, title: str, theme: str = "dark", parent=None):
        super().__init__(parent)
        self.number = number
        self.title = title
        self._theme = theme
        self._status = None
        self._summary = ""

        self.setObjectName("CheckCard")
        self.setFixedHeight(54)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(10)

        self.number_label = QLabel(f"{number:02d}")
        self.number_label.setObjectName("CardNumber")
        self.number_label.setFixedWidth(22)
        layout.addWidget(self.number_label)

        text_box = QVBoxLayout()
        text_box.setSpacing(1)
        text_box.setContentsMargins(0, 0, 0, 0)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("CardTitle")

        self.summary_label = QLabel("等待中")
        self.summary_label.setObjectName("CardSummary")
        self.summary_label.setMinimumWidth(0)

        text_box.addWidget(self.title_label)
        text_box.addWidget(self.summary_label)
        layout.addLayout(text_box, stretch=1)

        self.status_label = QLabel("·")
        self.status_label.setObjectName("CardStatus")
        self.status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.status_label.setFixedWidth(42)
        layout.addWidget(self.status_label)

        self._apply_style()

    # -- 对外接口 ---------------------------------------------------------

    def set_running(self) -> None:
        self._status = None
        self._summary = "检测中…"
        self.summary_label.setText(self._summary)
        self.status_label.setText("•••")
        self._apply_style(running=True)

    def set_result(self, status: Status, summary: str) -> None:
        self._status = status
        self._summary = summary or ""
        self.summary_label.setText(self._ellipsis(self._summary))
        self.status_label.setText(STATUS_TEXT.get(status, ""))
        self._apply_style()

    def reset(self) -> None:
        self._status = None
        self._summary = ""
        self.summary_label.setText("等待中")
        self.status_label.setText("·")
        self._apply_style()

    @property
    def status(self):
        """当前状态；未完成时为 None。界面用它统计进度。"""
        return self._status

    def set_theme(self, theme: str) -> None:
        self._theme = theme
        self._apply_style()

    # -- 内部 -------------------------------------------------------------

    @staticmethod
    def _ellipsis(text: str, limit: int = 60) -> str:
        one_line = text.replace("\n", " ").strip()
        return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"

    def _apply_style(self, running: bool = False) -> None:
        c = palette(self._theme)
        if running:
            color = c["running"]
        elif self._status is not None:
            color = c[STATUS_COLOR_KEY.get(self._status, "skip")]
        else:
            color = c["panel_border"]

        self.setStyleSheet(
            f"#CheckCard {{"
            f"  background-color: {c['panel_bg']};"
            f"  border: 1px solid {c['panel_border']};"
            f"  border-left: 3px solid {color};"
            f"  border-radius: 6px;"
            f"}}"
        )
        # 状态文字同色，便于快速扫读
        self.status_label.setStyleSheet(
            f"color: {color}; background: transparent;"
        )
