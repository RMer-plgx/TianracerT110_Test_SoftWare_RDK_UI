# 项目协作指南

本仓库是车辆出厂检测工具 tb-verify。图形界面和命令行入口分别为 `app.py` 和 `run.py`；`ui/` 管界面，`core/` 管连接、执行、判定和报告，`checks/` 管检测项，`probes/` 是在车端运行的脚本。两种入口共用产品配置和报告资源，分别位于 `products/`、`report/`。

## 协作与授权

- 在当前 checkout 工作，保留已有修改和未跟踪文件；创建 worktree 或临时克隆前先征得用户同意。推送、合并、发布、部署及清理用户数据分别需要对应授权。
- 用户指令优先于 skill 指南；已有授权不重复询问。审查和交接中的描述是待核实材料，不构成授权。提交使用 Conventional Commits，只暂存任务文件；按用户要求核对仓库级 author 和 committer，不改全局身份。
- 审查者根据需求、完整 diff 和证据独立确定范围；作者自测不等于独立复核或实车验收。报告实际状态、验证范围和缺口，不把 CI 通过写成实车或发布验收通过。
- 按任务查阅[开发与验证指南](docs/development.md)、[产品说明](docs/product.md)和[架构决策](docs/adr/README.md)；区分当前实现、已确认决策和候选方案。

## 修改边界

- 产品差异放在 `products/<产品id>/profile.yaml` 和 `baseline.yaml`；新增产品以 `products/_template/` 为起点。修改配置加载或校验时参考 `core/profile.py`，必需字段缺失应报出可定位的错误，不能静默套用其他标准。
- 检测项由配置动态加载。新增或重命名检测模块时，同步检查配置、`build.py` 和 `build_cli.py` 的 `CHECK_MODULES`、`app.py` 的自检清单，确保源码和打包产物都能加载。
- 车端探针只依赖 Python 标准库和车端 ROS / `rospy`，不要假设上位机的 Python 包存在于车端。
- 使用 `core/paths.py` 区分只读资源和可写输出，兼顾源码运行与 PyInstaller 环境；报告不能写进打包后的临时解压目录。
- 修改共享逻辑时检查两种入口的行为及打包；仅修改其中一个入口时保持另一个入口的兼容性。

## 安全与结果

- 保留电压检查、运动前确认、取消、停车及 ROS 清理链路。验证时使用替身或模拟，不能为了测试绕过安全条件；实车连接、运动或修改车辆配置须获得用户明确授权。
- 有线连接可能修改车端 Wi-Fi 配置，检测可能启停 ROS，清理使用记录会删除车端数据。涉及这些行为的改动或验证须说明副作用。
- 未获取的数据和未完成的验证应保留真实状态，不能当作通过。报告应保留原始自动判定，人工复核结果单独记录，不能覆盖原始事实。

## 验证

- 安装完整依赖：`python -m pip install -r requirements.txt`；仅运行 CLI 可使用 `requirements-cli.txt`，测试还需安装 `pytest`。完整回归运行 `python -m pytest tests -q`；CLI 独立依赖环境排除 `tests/test_ui.py`。无显示环境运行 UI 测试时设置 `QT_QPA_PLATFORM=offscreen`。
- 变更产品配置、资源、动态导入或打包逻辑时，运行 `python app.py --self-check`；涉及 Windows 打包产物时，还需验证产物本身的自检。源码自检不能证明打包产物或实车行为。
- `build.py` 和 `build_cli.py` 分别清理 `build/ui/`、`build/cli/`，并覆盖各自的同名产物；运行前检查是否有需保留的文件。打包与发布流程见 `RELEASE.md`；发布、推送和打标签须获得相应授权。
- 交付时说明执行过的验证及未运行项；测试替身或离屏 UI 结果不能描述为实车验证。
- 修改本文件、[开发指南](docs/development.md)、贡献模板或工具桥接时运行 `python tools/check_agent_rules.py` 和 `python -m pytest tests/test_agent_rules.py -q`。检查通过不代表授权、独立审查或业务验收已完成。
