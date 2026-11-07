"""Agent 规则检查器的正常与失败路径。"""

import subprocess
import sys
from pathlib import Path

import pytest

from tools.check_agent_rules import check


@pytest.fixture
def rules_repo(tmp_path):
    files = {
        "AGENTS.md": "# 规则\n## 协作与授权\n## 修改边界\n## 安全与结果\n## 验证\n[指南](docs/development.md#按改动选择验证)\n",
        "CLAUDE.md": "@AGENTS.md\n",
        "GEMINI.md": "@AGENTS.md\n",
        "docs/development.md": "# 开发\n## 按改动选择验证\n[规则](../AGENTS.md)\n",
        ".github/ISSUE_TEMPLATE/task.md": "[指南](../../docs/development.md)\n",
        ".github/pull_request_template.md": "[指南](../docs/development.md)\n",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


def test_valid_rules_and_code_blocks_are_accepted(rules_repo):
    with (rules_repo / "AGENTS.md").open("a", encoding="utf-8") as f:
        f.write("\n```md\n[示例](不存在.md)\n```\n")
    assert check(rules_repo) == []


@pytest.mark.parametrize("link,expected", [
    ("docs/missing.md", "链接目标不存在"),
    ("docs/development.md#不存在", "标题锚点不存在"),
    ("../outside.md", "链接不在仓库内"),
    ("docs/%ZZ.md", "链接编码无效"),
])
def test_invalid_links_are_rejected(rules_repo, link, expected):
    with (rules_repo / "AGENTS.md").open("a", encoding="utf-8") as f:
        f.write(f"\n[错误]({link})\n")
    assert any(expected in problem for problem in check(rules_repo))


def test_missing_source_and_changed_bridge_are_rejected(rules_repo):
    (rules_repo / "CLAUDE.md").write_text("其他规则\n", encoding="utf-8")
    (rules_repo / ".github/pull_request_template.md").unlink()
    problems = check(rules_repo)
    assert any("CLAUDE.md" in problem for problem in problems)
    assert any("pull_request_template.md" in problem for problem in problems)


def test_link_through_symlink_cannot_escape_repository(rules_repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "other.md"
    outside.write_text("# 外部\n", encoding="utf-8")
    link = rules_repo / "docs" / "linked.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")
    with (rules_repo / "AGENTS.md").open("a", encoding="utf-8") as f:
        f.write("\n[越界](docs/linked.md)\n")
    assert any("链接不在仓库内" in problem for problem in check(rules_repo))


def test_cli_returns_nonzero_for_broken_rules(rules_repo):
    (rules_repo / "GEMINI.md").write_text("其他规则\n", encoding="utf-8")
    script = Path(__file__).resolve().parent.parent / "tools/check_agent_rules.py"
    result = subprocess.run([sys.executable, str(script), "--root", str(rules_repo)],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode != 0
    assert "GEMINI.md" in result.stderr


class TestInlineCodeLinks:
    """反引号包裹的链接文字不能被跳过（2026-11-07 审查 M1）。

    旧实现先 `re.sub(r"`[^`\n]+`", "", text)` 删掉行内代码，
    `[`foo`](bar)` 于是变成 `[](bar)`；LINK 要求 `[ ]` 间至少 1 个字符，
    空的不匹配 —— 整条链接被静默跳过。实测全仓只有 1 条被跳过，
    而它就是真死链。
    """

    def test_backticked_link_text_is_still_checked(self, rules_repo):
        with (rules_repo / "AGENTS.md").open("a", encoding="utf-8") as f:
            f.write("\n[`a.patch`](docs/missing.patch)\n")
        problems = check(rules_repo)
        assert any("链接目标不存在" in p for p in problems), (
            f"反引号链接被跳过了：{problems}"
        )

    def test_backticked_link_text_valid_target_passes(self, rules_repo):
        with (rules_repo / "AGENTS.md").open("a", encoding="utf-8") as f:
            f.write("\n[`指南`](docs/development.md)\n")
        assert check(rules_repo) == []

    def test_code_span_containing_brackets_does_not_confuse_parser(self, rules_repo):
        """行内代码里的 `](` 不应被当成链接。"""
        with (rules_repo / "AGENTS.md").open("a", encoding="utf-8") as f:
            f.write("\n写法是 `[a](b)`，这里不是链接。\n")
        assert check(rules_repo) == []


class TestWholeRepoScan:
    """文档链接要全仓检查，不能只扫 SOURCES 四个文件。

    ADR / design / product 都在 docs/ 下，之前完全不检查；
    死链（M2）就是因此溜过去的。
    """

    def test_dead_link_in_docs_is_found(self, rules_repo):
        adr = rules_repo / "docs" / "adr" / "0001-test.md"
        adr.parent.mkdir(parents=True, exist_ok=True)
        adr.write_text("# ADR\n[x](../patches/nope.patch)\n", encoding="utf-8")
        problems = check(rules_repo)
        assert any("0001-test.md" in p and "链接目标不存在" in p for p in problems)

    def test_good_link_in_docs_passes(self, rules_repo):
        adr = rules_repo / "docs" / "adr" / "0002-ok.md"
        adr.parent.mkdir(parents=True, exist_ok=True)
        adr.write_text("# ADR\n[开发](../development.md)\n", encoding="utf-8")
        assert check(rules_repo) == []

    def test_build_outputs_are_skipped(self, rules_repo):
        """dist / .codewhale 等产物目录里的死链不该报。"""
        for sub in ("dist", ".codewhale", "node_modules"):
            d = rules_repo / sub
            d.mkdir(parents=True, exist_ok=True)
            (d / "old.md").write_text("[x](missing.md)\n", encoding="utf-8")
        assert check(rules_repo) == []

    def test_anchor_in_docs_is_checked(self, rules_repo):
        adr = rules_repo / "docs" / "adr" / "0003-anchor.md"
        adr.parent.mkdir(parents=True, exist_ok=True)
        adr.write_text("# 标题\n[跳转](../development.md#不存在的小节)\n",
                       encoding="utf-8")
        problems = check(rules_repo)
        assert any("标题锚点不存在" in p for p in problems)
