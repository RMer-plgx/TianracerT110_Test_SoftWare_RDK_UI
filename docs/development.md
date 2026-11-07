# 开发与验证指南

## 协作与交付

先确认当前分支、工作区状态、任务范围和已有授权。保留无关改动；本地修改、提交、推送、合并、发布、实车操作和清理是不同动作。发生外部写入后回读目标、状态和相关 SHA。具体仓库边界见 [Agent 规则](../AGENTS.md)。

开始实现前明确问题、预期行为和可观察的完成条件；Issue 和 PR 模板可用于记录，不因模板内容自动获得操作授权。审查者从实际需求和完整差异确定范围，独立报告发现、证据缺口和实际看过的内容；作者的自测不冒充独立审查。结论随被审查 SHA 变化而失效。

## MyGo 插件平台实施

新平台按[技术方案](design/mygo-plugin-platform.md)的 P0–P5 推进，插件与运行时遵守[接口契约](design/plugin-api-v1.md)，界面参考 [UI/UX 方案](design/mygo-ui-ux.md)。方向已确认，本仓库尚未提供对应 Go 工程、SDK 或执行命令；实施后将实际命令与平台条件补入本指南，不能把文档示例当作现有能力。

每阶段保留源码 SHA、插件与基线摘要、替身/实车区别、平台和失败路径证据。插件加载或无头测试通过不等于目标系统、设备安全或产品验收完成。新增必需能力会影响协议、适配器、结果完整性和界面，需要共同验证。当前 Python 代码仍按下表验证，新平台文档不改变现有运行行为。

## 按改动选择验证

安装运行依赖 `python -m pip install -r requirements.txt` 和测试依赖 `python -m pip install pytest`；命令从仓库根目录运行。

| 改动 | 最小验证 |
| --- | --- |
| Agent 规则、桥接、贡献模板或本指南 | `python tools/check_agent_rules.py`；`python -m pytest tests/test_agent_rules.py -q`；`git diff --check` |
| 产品配置、检测项、共享核心或报告 | 对应测试和 `python -m pytest tests/test_checks_smoke.py tests/test_harness_contract.py -q`；确认受影响的图形界面行为 |
| UI 或连接流程 | 对应 UI／连接测试；无显示环境设 `QT_QPA_PLATFORM=offscreen` |
| 资源、动态模块、打包逻辑 | `python app.py --self-check` 和 `python -m pytest tests/test_harness_contract.py tests/test_build_entrypoints.py -q`；产物交付前还须在目标系统检查实际产物 |
| 发布清单或发布说明 | `python -m pytest tests/test_prepare_release.py -q`；按[发布流程](../RELEASE.md)核对版本、源码、附件和实际发布状态 |

完整回归使用 `python -m pytest tests -q`。规则检查器只校验仓库内文档链接、标题和工具桥接；产品清单检查覆盖已发现的产品配置与入口打包清单，不能证明所有运行分支或真实产物可用。

## 车辆与证据

自动测试使用替身，不连接车辆。实车连接、运动及修改车端配置需要用户明确授权；运动前验证电压、人员与场地，并保留停止和清理路径。采集窗口里没有数据只说明该窗口未观察到，不能推断固件或硬件根因。测试记录和报告中的凭据、车辆信息及现场数据按授权处理，不随意提交。

仓库提供未启用的 [CI 工作流模板](../.github/ci.yml.example)，包含规则检查、配置与打包清单契约、Python 回归与界面离屏测试和源码自检。模板不位于 GitHub Actions 工作流目录，不会自动运行；启用前需核对运行环境和授权。它不启用分支保护，不构建 Windows 产物、不连接实车，也不自动发布。发布和实际产物验证见[发布流程](../RELEASE.md)。
