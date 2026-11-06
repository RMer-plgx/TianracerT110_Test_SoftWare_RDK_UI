"""版本号。

规则：语义化版本（SemVer）MAJOR.MINOR.PATCH

  MAJOR  不兼容的变更（配置格式、报告结构、判定语义反转）
  MINOR  向后兼容的新功能
  PATCH  向后兼容的缺陷修复

发布时更新这里的 __version__，并按 RELEASE.md 确认版本、标签和发布说明。

版本号会进报告（report.tool_version），所以拿到一份报告就能反查
是哪一版软件生成的 —— 这一点在多产品、多现场的情况下尤其重要。
"""

__version__ = "2.3.0"

# 发布批次代号，便于口头沟通（"那个 2.1 的版本"）
CODENAME = "多产品架构"

__all__ = ["__version__", "CODENAME"]
