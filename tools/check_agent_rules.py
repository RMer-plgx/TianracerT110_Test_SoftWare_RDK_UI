#!/usr/bin/env python3
"""只读检查 Agent 入口、工具桥接与协作文档的仓库内链接。"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

SOURCES = (
    "AGENTS.md", "docs/development.md",
    ".github/ISSUE_TEMPLATE/task.md", ".github/pull_request_template.md",
)
# 全仓链接扫描的排除项。
# 只扫 SOURCES 四个文件会漏掉 docs/ 下的全部文档 —— 死链正是这么
# 活下来的（2026-11-07 审查 M1）。但要排除构建产物、依赖与历史快照：
#   dist/            打包产物，内容随构建变
#   .codewhale/      历史审查快照，当时路径不代表现在有效
#   platform/product-plugins/  插件包，资源由打包时生成
SCAN_SKIP = ("dist", ".git", "node_modules", ".codewhale",
             "product-plugins", "__pycache__", ".pytest_cache")
SECTIONS = ("协作与授权", "修改边界", "安全与结果", "验证")
LINK = re.compile(r"!?\[[^\]\n]*\]\(([^)\n]+)\)")
# 文字部分用 `*` 而非 `+`：
#   ![](img.png)  图片是最常见的空文字形式，`+` 会整个漏掉
#   [](x.md)      空文字链接（少见但合法）
# 覆盖率边界：本检查器只管**行内链接**。引用式（`[t][r]` + `[r]: url`）
# 与 HTML `<a href>` 不检查 —— 若日后引入这两种写法，需先扩展这里。
#
# 行内代码：把内容换成等长占位符，**不能整段删除**。
# 删除 `[`foo`](bar)` 会变成 `[](bar)`；用 `*` 后反而会被当成正常链接，
# 但占位符仍能避免行内代码里的 `](` 被误判成链接（2026-11-07 审查 M1）。
INLINE_CODE = re.compile(r"`[^`\n]*`")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def prose(text: str) -> str:
    lines = []
    fence = None
    for line in text.splitlines():
        match = FENCE.match(line)
        if match:
            mark = match.group(1)
            if fence is None:
                fence = mark
            elif mark[0] == fence[0] and len(mark) >= len(fence):
                fence = None
            continue
        if fence is None:
            lines.append(line)
    return "\n".join(lines)


def blank_inline_code(text: str) -> str:
    """把行内代码内容换成等长占位符，保留外围链接结构。

    保持长度不变是为了让后续报错的位置与实际行保持对应。
    替换成 `x` 而不是删除，还能顺带消除行内代码里的 `]`、`(`
    对链接正则的干扰。
    """
    return INLINE_CODE.sub(lambda m: "x" * len(m.group(0)), text)


def heading_anchors(text: str) -> set[str]:
    counts: dict[str, int] = {}
    result = set()
    for line in prose(text).splitlines():
        match = HEADING.match(line)
        if not match:
            continue
        heading = match.group(1).lower().replace("`", "")
        slug = "".join(c for c in heading if c in "-_ " or
                       unicodedata.category(c)[0] in ("L", "N", "M"))
        slug = slug.replace(" ", "-")
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        result.add(f"{slug}-{count}" if count else slug)
    return result


def markdown_files(root: Path) -> list[Path]:
    """仓库内所有该检查的 Markdown。

    用 `os.walk` 就地剪枝而不是 `rglob` + 过滤：后者会先把 `dist/`
    （里面装的是带 report.md 的历史报告）、`node_modules/` 整个走完
    再丢弃，随构建次数线性变慢（Round 13 审查 R13-4）。
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        # 就地修改 dirnames，walk 就不会下去
        dirnames[:] = [d for d in dirnames if d not in SCAN_SKIP]
        for name in filenames:
            if name.endswith(".md"):
                out.append(Path(dirpath) / name)
    return sorted(out)


def check_links(root: Path, path: Path) -> list[str]:
    """校验单个 Markdown 的相对链接与锚点。"""
    name = str(path.relative_to(root)).replace("\\", "/")
    errors: list[str] = []
    text = prose(path.read_text(encoding="utf-8"))
    # 行内代码里的 `](` 不是链接；占位符保留长度但不带语义。
    for match in LINK.finditer(blank_inline_code(text)):
        link = match.group(1).strip().split(' "', 1)[0].strip("<>")
        if re.search(r"%(?![0-9a-fA-F]{2})", link):
            errors.append(f"{name}: 链接编码无效 {link}")
            continue
        parts = urlsplit(link)
        if parts.scheme in ("http", "https", "mailto"):
            continue
        if parts.scheme or parts.netloc or parts.path.startswith("/"):
            errors.append(f"{name}: 链接不在仓库内 {link}")
            continue
        target = (path.parent / unquote(parts.path)).resolve() if parts.path else path.resolve()
        if not target.is_relative_to(root):
            errors.append(f"{name}: 链接不在仓库内 {link}")
        elif not target.is_file():
            errors.append(f"{name}: 链接目标不存在 {link}")
        elif (parts.fragment and target.suffix.lower() == ".md"
              and unquote(parts.fragment) not in heading_anchors(target.read_text(encoding="utf-8"))):
            errors.append(f"{name}: 标题锚点不存在 {link}")
    return errors


def check(root: Path) -> list[str]:
    root = root.resolve()
    errors: list[str] = []
    for name in ("CLAUDE.md", "GEMINI.md"):
        path = root / name
        if (not path.is_file() or not path.resolve().is_relative_to(root)
                or path.read_text(encoding="utf-8").strip() != "@AGENTS.md"):
            errors.append(f"{name}: 必须仅引用 @AGENTS.md，且位于仓库内")

    for name in SOURCES:
        path = root / name
        if not path.is_file() or not path.resolve().is_relative_to(root):
            errors.append(f"{name}: 缺少仓库内的文档入口")
            continue
        text = prose(path.read_text(encoding="utf-8"))
        if name == "AGENTS.md":
            for section in SECTIONS:
                if f"## {section}" not in text.splitlines():
                    errors.append(f"{name}: 缺少章节 {section}")
    # 全仓链接检查（含入口文件；SOURCES 也要查链接，只是额外要求章节）。
    for path in markdown_files(root):
        errors.extend(check_links(root, path))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    problems = check(args.root)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print("Agent 规则：文档链接、标题和工具桥接通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
