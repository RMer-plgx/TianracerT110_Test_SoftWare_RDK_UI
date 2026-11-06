# 开发与验证指南

## 协作与交付

先确认当前分支、工作区状态、任务范围和已有授权。保留无关改动；本地修改、提交、推送、合并、发布、实车操作和清理是不同动作。发生外部写入后回读目标、状态和相关 SHA。具体仓库边界见 [Agent 规则](../AGENTS.md)。

开始实现前明确问题、预期行为和可观察的完成条件；Issue 和 PR 模板可用于记录，不因模板内容自动获得操作授权。审查者从实际需求和完整差异确定范围，独立报告发现、证据缺口和实际看过的内容；作者的自测不冒充独立审查。结论随被审查 SHA 变化而失效。

## 按改动选择验证

安装完整运行依赖 `python -m pip install -r requirements.txt` 和测试依赖 `python -m pip install pytest`。仅验证 CLI 时可使用 `requirements-cli.txt`；命令从仓库根目录运行。

| 改动 | 最小验证 |
| --- | --- |
| Agent 规则、桥接、贡献模板或本指南 | `python tools/check_agent_rules.py`；`python -m pytest tests/test_agent_rules.py -q`；`git diff --check` |
| 产品配置、检测项、共享核心或报告 | 对应测试和 `python -m pytest tests/test_checks_smoke.py tests/test_harness_contract.py -q`；确认两个入口受影响的行为 |
| UI 或连接流程 | 对应 UI／连接测试；无显示环境设 `QT_QPA_PLATFORM=offscreen` |
| CLI 入口 | `python -m pytest tests/test_cli_entry.py -q`，以及 `python run.py --help` |
| 资源、动态模块、打包逻辑 | `python app.py --self-check` 和 `python -m pytest tests/test_harness_contract.py tests/test_build_entrypoints.py -q`；产物交付前还须在目标系统检查实际产物 |
| 发布清单或发布说明 | `python -m pytest tests/test_prepare_release.py -q`；按[发布流程](../RELEASE.md)核对版本、源码、附件和实际发布状态 |

完整回归使用 `python -m pytest tests -q`。仅安装 CLI 依赖时，使用 `python -m pytest tests -q --ignore=tests/test_ui.py`。规则检查器只校验仓库内文档链接、标题和工具桥接；产品清单检查覆盖已发现的产品配置与入口打包清单，不能证明所有运行分支或真实产物可用。

## 车辆与证据

自动测试使用替身，不连接车辆。实车连接、运动及修改车端配置需要用户明确授权；运动前验证电压、人员与场地，并保留停止和清理路径。采集窗口里没有数据只说明该窗口未观察到，不能推断固件或硬件根因。测试记录和报告中的凭据、车辆信息及现场数据按授权处理，不随意提交。

仓库提供未启用的 [CI 工作流模板](../.github/ci.yml.example)，包含规则检查、配置与打包清单契约、CLI 测试、界面离屏测试和源码自检。模板不位于 GitHub Actions 工作流目录，不会自动运行；启用前需核对运行环境和授权。它不启用分支保护，不构建 Windows 产物、不连接实车，也不自动发布。发布和实际产物验证见[发布流程](../RELEASE.md)。
