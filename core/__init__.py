"""版本号。

规则：语义化版本（SemVer）MAJOR.MINOR.PATCH

  MAJOR  不兼容的变更（配置格式、报告结构、判定语义反转）
  MINOR  向后兼容的新功能
  PATCH  向后兼容的缺陷修复

**每次发版必须同时做三件事**：
  1. 改这里的 __version__
  2. 在 CHANGELOG.md 顶部加一节
  3. git tag -a v<版本> -m "..." 并推送

版本号会进报告（report.tool_version），所以拿到一份报告就能反查
是哪一版软件生成的 —— 这一点在多产品、多现场的情况下尤其重要。
"""

__version__ = "2.3.0"

# 发布批次代号，便于口头沟通（"那个 2.1 的版本"）
CODENAME = "多产品架构"

# CHANGELOG 里的版本号必须与这里一致，由测试保证
__all__ = ["__version__", "CODENAME"]
