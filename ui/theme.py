"""界面主题：深色 / 浅色两套 QSS。

品牌色取自 logo 的金棕色，深色主题下同色系做高亮。
所有颜色集中在这里，换主题只需要改这两份字符串，不用翻界面代码。
"""

# 品牌金棕色，与 assets/app.ico 同色
BRAND = "#A87E51"
BRAND_LIGHT = "#C9A374"
BRAND_DARK = "#8A6540"

DARK = {
    "name": "dark",
    "window_bg": "#1C1B19",
    "panel_bg": "#252321",
    "panel_border": "#3A3632",
    "text": "#EDEAE5",
    "text_muted": "#9A938A",
    "accent": BRAND,
    "input_bg": "#1A1917",
    "input_border": "#3A3632",
    "pass": "#4CAF7D",
    "warn": "#E0A030",
    "fail": "#E05252",
    "skip": "#7A7268",
    # 中间态：已测一部分，等落地段。青蓝色，与告警黄、跳过灰都能区分
    "pending": "#4A9BC4",
    "running": BRAND_LIGHT,
}

LIGHT = {
    "name": "light",
    "window_bg": "#F5F3F0",
    "panel_bg": "#FFFFFF",
    "panel_border": "#DED8D0",
    "text": "#2A2622",
    "text_muted": "#7A7268",
    "accent": BRAND,
    "input_bg": "#FFFFFF",
    "input_border": "#CFC8BE",
    "pass": "#1E8E4E",
    "warn": "#B47600",
    "fail": "#C0332E",
    "skip": "#8A8278",
    "pending": "#1F6F94",
    "running": BRAND_DARK,
}


def palette(name: str) -> dict:
    return DARK if name == "dark" else LIGHT


def stylesheet(name: str) -> str:
    """生成整份 QSS。"""
    c = palette(name)
    return f"""
    QWidget {{
        background-color: {c['window_bg']};
        color: {c['text']};
        font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
        font-size: 13px;
    }}

    QMainWindow, QDialog {{
        background-color: {c['window_bg']};
    }}

    /* ---------- 面板 ---------- */
    #SidePanel, #LogPanel, #ResultPanel {{
        background-color: {c['panel_bg']};
        border: 1px solid {c['panel_border']};
        border-radius: 10px;
    }}

    #HeaderTitle {{
        font-size: 19px;
        font-weight: 600;
        color: {c['text']};
        background: transparent;
    }}

    #HeaderSub {{
        font-size: 12px;
        color: {c['text_muted']};
        background: transparent;
    }}

    #SectionTitle {{
        font-size: 12px;
        font-weight: 600;
        color: {c['text_muted']};
        background: transparent;
        padding: 2px 0 6px 0;
    }}

    /* ---------- 输入控件 ---------- */
    QLineEdit, QComboBox {{
        background-color: {c['input_bg']};
        border: 1px solid {c['input_border']};
        border-radius: 6px;
        padding: 7px 10px;
        color: {c['text']};
        selection-background-color: {c['accent']};
    }}
    QLineEdit:focus, QComboBox:focus {{
        border: 1px solid {c['accent']};
    }}
    QLineEdit:disabled {{
        color: {c['text_muted']};
    }}

    QComboBox::drop-down {{
        border: none;
        width: 22px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {c['panel_bg']};
        border: 1px solid {c['panel_border']};
        selection-background-color: {c['accent']};
        color: {c['text']};
        outline: none;
    }}

    QCheckBox {{
        background: transparent;
        spacing: 8px;
    }}
    QCheckBox::indicator {{
        width: 16px;
        height: 16px;
        border: 1px solid {c['input_border']};
        border-radius: 4px;
        background: {c['input_bg']};
    }}
    QCheckBox::indicator:checked {{
        background: {c['accent']};
        border: 1px solid {c['accent']};
    }}

    /* ---------- 按钮 ---------- */
    QPushButton {{
        background-color: {c['accent']};
        color: #FFFFFF;
        border: none;
        border-radius: 6px;
        padding: 9px 18px;
        font-weight: 600;
    }}
    QPushButton:hover {{
        background-color: {c['accent']};
    }}
    QPushButton:disabled {{
        background-color: {c['panel_border']};
        color: {c['text_muted']};
    }}

    QPushButton#Ghost {{
        background: transparent;
        color: {c['text']};
        border: 1px solid {c['panel_border']};
    }}
    QPushButton#Ghost:hover {{
        border: 1px solid {c['accent']};
        color: {c['accent']};
    }}

    QPushButton#Danger {{
        background-color: {c['fail']};
    }}

    /* ---------- 检测项卡片 ---------- */
    #CheckCard {{
        background-color: {c['panel_bg']};
        border: 1px solid {c['panel_border']};
        border-left: 3px solid {c['panel_border']};
        border-radius: 6px;
    }}
    #CardNumber {{
        color: {c['text_muted']};
        font-size: 11px;
        background: transparent;
    }}
    #CardTitle {{
        font-size: 13px;
        font-weight: 600;
        background: transparent;
    }}
    #CardSummary {{
        color: {c['text_muted']};
        font-size: 11px;
        background: transparent;
    }}
    #CardStatus {{
        font-size: 11px;
        font-weight: 600;
        background: transparent;
    }}

    /* ---------- 日志 ---------- */
    QPlainTextEdit#LogView {{
        background-color: {c['input_bg']};
        border: 1px solid {c['panel_border']};
        border-radius: 8px;
        color: {c['text']};
        font-family: "Cascadia Mono", Consolas, monospace;
        font-size: 12px;
        padding: 8px;
    }}

    /* ---------- 进度条 ---------- */
    QProgressBar {{
        background-color: {c['input_bg']};
        border: 1px solid {c['panel_border']};
        border-radius: 5px;
        min-height: 10px;
        max-height: 10px;
        text-align: center;
        color: transparent;
    }}
    QProgressBar::chunk {{
        background-color: {c['accent']};
        border-radius: 4px;
    }}

    /* ---------- 滚动条 ---------- */
    QScrollArea {{ border: none; background: transparent; }}
    QScrollBar:vertical {{
        background: transparent;
        width: 6px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {c['panel_border']};
        border-radius: 3px;
        min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {c['accent']};
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

    /* ---------- 其它 ---------- */
    QLabel {{ background: transparent; }}
    QMessageBox {{ background-color: {c['panel_bg']}; }}
    QToolTip {{
        background-color: {c['panel_bg']};
        color: {c['text']};
        border: 1px solid {c['accent']};
        padding: 4px;
    }}
"""
