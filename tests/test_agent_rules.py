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
