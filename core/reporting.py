"""报告渲染：把 Report 对象写成 markdown / html 文件。"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .paths import resource_path
from .verdict import Report


def _env(autoescape: bool = False) -> Environment:
    """构造 Jinja 环境。

    autoescape 对 HTML 必须为 True：报告里要插入车辆命令的输出
    （hostname、环境变量值、git remote、frame_id、ls -l 结果），
    这些内容不完全受控。若某个环境变量值是 `<script>...</script>`，
    报告在浏览器里打开就会执行 —— 而 PDF 导出用的是同一个内核。

    Markdown 必须为 False：`<` 在 Markdown 里是普通字符，
    转成 `&lt;` 会直接显示成乱码。
    """
    template_dir = resource_path("report")
    environment = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=autoescape,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    environment.filters["pct"] = lambda v: f"{float(v) * 100:.0f}%"
    return environment


def _safe_filename_part(text: str) -> str:
    """把任意字符串压成可安全放进文件名/JS 字符串的形式。

    只保留字母、数字、点、横线、下划线。host 目前经 UI 的 `_valid_host`
    校验过，但 CLI 版的 `--host` 参数不经过那里，所以统一在这里消毒。
    """
    cleaned = "".join(
        c for c in (text or "") if c.isalnum() or c in ".-_"
    )
    return cleaned or "unknown"


# 报告内「下载 PDF」需要的两个库。复制到每份报告目录旁，
# 让报告文件夹自包含 —— 可以整个 zip 发给出货人员，离线也能导出。
VENDOR_FILES = ("jspdf.umd.min.js", "html2canvas.min.js")


def _copy_vendor(record_dir: Path) -> list:
    """把 PDF 库复制到报告目录。失败不阻断报告生成。"""
    import shutil

    copied = []
    source_dir = resource_path("report/vendor")
    for name in VENDOR_FILES:
        src = source_dir / name
        if not src.is_file():
            continue
        try:
            shutil.copy2(src, record_dir / name)
            copied.append(name)
        except OSError:
            # 磁盘满/只读介质：报告仍要生成，只是没有 PDF 按钮
            pass
    return copied


def render(report: Report, record_dir: Path) -> Tuple[Path, Path]:
    """写 report.md 与 report.html，返回两个路径。

    所有输出都显式用 utf-8，避免 Windows 默认 GBK 把中文写坏。

    同时把 PDF 导出用的两个 JS 库复制到目录旁（若存在）。
    报告 HTML 里以相对路径引用，因此整个文件夹可以搬走/压缩/邮件发送。
    """
    md_path = record_dir / "report.md"
    md_text = _env(autoescape=False).get_template("md.j2").render(report=report)
    md_path.write_text(md_text, encoding="utf-8")

    # 先准备库文件，模板里据此决定是否显示「下载 PDF」按钮
    available = _copy_vendor(record_dir)

    html_path = record_dir / "report.html"
    html_text = _env(autoescape=True).get_template("html.j2").render(
        report=report, pdf_vendor=available,
        safe_host=_safe_filename_part(report.host),
    )
    html_path.write_text(html_text, encoding="utf-8")

    return md_path, html_path
