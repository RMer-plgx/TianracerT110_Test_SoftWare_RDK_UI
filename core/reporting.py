"""报告渲染：把 Report 对象写成 markdown / html 文件。"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .paths import resource_path
from .verdict import Report


def _env() -> Environment:
    template_dir = resource_path("report")
    environment = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    environment.filters["pct"] = lambda v: f"{float(v) * 100:.0f}%"
    return environment


def render(report: Report, record_dir: Path) -> Tuple[Path, Path]:
    """写 report.md 与 report.html，返回两个路径。

    所有输出都显式用 utf-8，避免 Windows 默认 GBK 把中文写坏。
    """
    environment = _env()

    md_path = record_dir / "report.md"
    md_text = environment.get_template("md.j2").render(report=report)
    md_path.write_text(md_text, encoding="utf-8")

    html_path = record_dir / "report.html"
    html_text = environment.get_template("html.j2").render(report=report)
    html_path.write_text(html_text, encoding="utf-8")

    return md_path, html_path
