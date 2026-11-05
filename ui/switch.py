"""胶囊型滑动开关。

设计要点（用户 2026-11-05 要求）：
- 胶囊外形（两端全圆角）
- 一明一暗：选中的一侧亮，未选中的一侧暗
- 滑动有动画，不是瞬移

为什么自绘而不是用两个 QPushButton：
QPushButton 的分段按钮做不出「一个滑块在轨道上滑过去」的效果。
滑动感需要连续的插值位置，只有自绘 + QPropertyAnimation 能做到。

动画用 QPropertyAnimation 驱动一个 0.0~1.0 的连续量 `offset`，
paintEvent 按它算滑块位置。这样窗口缩放时也自然跟随。
"""

from __future__ import annotations

from PyQt5.QtCore import (
    QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt, pyqtProperty,
    pyqtSignal,
)
from PyQt5.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap
from PyQt5.QtWidgets import QWidget


class CapsuleSwitch(QWidget):
    """两档胶囊开关。左 = 0，右 = 1。"""

    changed = pyqtSignal(int)

    # 尺寸常量：写死是有意的，胶囊的圆角半径必须等于高度一半，
    # 随内容自适应反而会变形。
    HEIGHT = 40
    PADDING = 4          # 滑块与轨道的间隙
    GAP = 8              # 图标与文字之间

    # 配色（深色主题）；浅色主题下由 set_colors 覆盖
    TRACK_OFF = QColor("#2A2724")
    TRACK_ON = QColor("#A87E51")
    KNOB = QColor("#F2EDE6")
    TEXT_ACTIVE = QColor("#2A2724")
    TEXT_IDLE = QColor("#9A938A")

    def __init__(self, left_text: str, right_text: str,
                 left_icon: str = "", right_icon: str = "",
                 parent=None):
        super().__init__(parent)
        self._left_text = left_text
        self._right_text = right_text
        self._left_pix = QPixmap(left_icon) if left_icon else QPixmap()
        self._right_pix = QPixmap(right_icon) if right_icon else QPixmap()
        self._index = 0
        self._offset = 0.0          # 0.0 = 全左, 1.0 = 全右
        self._anim = QPropertyAnimation(self, b"offset", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(self.HEIGHT)
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(),
                           self.sizePolicy().verticalPolicy())

    # -- 动画属性 ---------------------------------------------------------

    def get_offset(self) -> float:
        return self._offset

    def set_offset(self, value: float) -> None:
        self._offset = max(0.0, min(1.0, float(value)))
        self.update()

    offset = pyqtProperty(float, fget=get_offset, fset=set_offset)

    # -- 对外接口 ---------------------------------------------------------

    @property
    def index(self) -> int:
        return self._index

    def set_index(self, index: int, animate: bool = True) -> None:
        """切换档位。animate=False 用于初始化（不要一开始就动）。"""
        index = 1 if index else 0
        if index == self._index and animate:
            return
        self._index = index
        target = float(index)
        if animate:
            self._anim.stop()
            self._anim.setStartValue(self._offset)
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._anim.stop()
            self.set_offset(target)
        self.changed.emit(index)

    def set_colors(self, track_off: str, track_on: str, knob: str,
                   text_active: str, text_idle: str) -> None:
        """按主题换色。"""
        self.TRACK_OFF = QColor(track_off)
        self.TRACK_ON = QColor(track_on)
        self.KNOB = QColor(knob)
        self.TEXT_ACTIVE = QColor(text_active)
        self.TEXT_IDLE = QColor(text_idle)
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(180, self.HEIGHT)

    # -- 交互 -------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        # 点左半边去左边，点右半边去右边
        half = self.width() / 2
        self.set_index(0 if event.x() < half else 1)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Left, Qt.Key_Right):
            self.set_index(0 if event.key() == Qt.Key_Left else 1)
        else:
            super().keyPressEvent(event)

    # -- 绘制 -------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        rect = QRectF(self.rect())
        radius = rect.height() / 2

        # 1. 轨道底色（暗）
        track = QPainterPath()
        track.addRoundedRect(rect, radius, radius)
        painter.fillPath(track, self.TRACK_OFF)

        # 2. 滑块（亮）—— 占一半宽度，按 offset 插值移动
        knob_w = (rect.width() - self.PADDING * 2) / 2
        knob_x = self.PADDING + self._offset * knob_w
        knob_rect = QRectF(knob_x, self.PADDING,
                           knob_w, rect.height() - self.PADDING * 2)
        knob = QPainterPath()
        knob_radius = knob_rect.height() / 2
        knob.addRoundedRect(knob_rect, knob_radius, knob_radius)
        painter.fillPath(knob, self.TRACK_ON)

        # 3. 两侧的文字与图标
        # 选中侧文字画在滑块上，用深色；未选中侧用浅色
        half = rect.width() / 2
        self._draw_side(painter, QRectF(0, 0, half, rect.height()),
                        self._left_pix, self._left_text,
                        active=self._offset < 0.5)
        self._draw_side(painter, QRectF(half, 0, half, rect.height()),
                        self._right_pix, self._right_text,
                        active=self._offset >= 0.5)

    def _draw_side(self, painter: QPainter, area: QRectF,
                   pix: QPixmap, text: str, active: bool) -> None:
        painter.save()

        # 选中/未选中的明暗：不只是颜色，未选中还降低不透明度，
        # 这样一眼能看出哪边是当前状态。
        painter.setOpacity(1.0 if active else 0.55)
        color = self.TEXT_ACTIVE if active else self.TEXT_IDLE

        font = QFont(painter.font())
        font.setPixelSize(13)
        font.setBold(active)
        painter.setFont(font)

        icon_size = 18 if not pix.isNull() else 0
        gap = self.GAP if (icon_size and text) else 0

        text_w = painter.fontMetrics().width(text) if text else 0
        total = icon_size + gap + text_w
        start_x = area.center().x() - total / 2
        cy = area.center().y()

        if icon_size:
            target = QRectF(start_x, cy - icon_size / 2, icon_size, icon_size)
            painter.drawPixmap(target.toRect(), pix)
            start_x += icon_size + gap

        if text:
            painter.setPen(color)
            text_rect = QRectF(start_x, area.top(), text_w + 2, area.height())
            painter.drawText(text_rect,
                             Qt.AlignVCenter | Qt.AlignLeft, text)

        painter.restore()


class ConnectionSwitch(CapsuleSwitch):
    """有线 / 无线 专用开关，带图标与主题配色。"""

    def __init__(self, icon_dir, parent=None):
        super().__init__(
            left_text="有线", right_text="无线",
            left_icon=str(icon_dir / "usb.png") if (icon_dir / "usb.png").exists() else "",
            right_icon=str(icon_dir / "wifi.png") if (icon_dir / "wifi.png").exists() else "",
            parent=parent,
        )
        self.setToolTip(
            "有线：USB-C 直连上位机，软件自动配 WiFi 后提示拔线\n"
            "无线：车已在 WiFi 上，直接填 IP"
        )

    def set_theme(self, theme: str) -> None:
        if theme == "dark":
            self.set_colors("#2A2724", "#A87E51", "#F2EDE6", "#2A2724", "#9A938A")
        else:
            self.set_colors("#E4DED4", "#8A6540", "#FFFFFF", "#FFFFFF", "#7A7268")
