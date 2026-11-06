#!/usr/bin/env python3
"""只读检查 Agent 入口、工具桥接与协作文档的仓库内链接。"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

SOURCES = (
    "AGENTS.md", "docs/development.md",
    ".github/ISSUE_TEMPLATE/task.md", ".github/pull_request_template.md",
)
SECTIONS = ("协作与授权", "修改边界", "安全与结果", "验证")
LINK = re.compile(r"!?\[[^\]\n]+\]\(([^)\n]+)\)")
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
        # 行内代码不是 Markdown 链接；只检查实际的相对链接。
        for match in LINK.finditer(re.sub(r"`[^`\n]+`", "", text)):
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
            elif parts.fragment and target.suffix.lower() == ".md" and unquote(parts.fragment) not in heading_anchors(target.read_text(encoding="utf-8")):
                errors.append(f"{name}: 标题锚点不存在 {link}")
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
