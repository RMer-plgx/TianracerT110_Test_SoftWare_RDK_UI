# 发布流程

tb-verify 的界面版和命令行版共用版本号、检测代码和配置，分别生成 Windows 产物。发布前确认目标仓库 `tianbot/tb-verify`、可见性、版本及授权。

1. 对照 `core/__init__.py` 和 `CHANGELOG.md` 检查版本号与变更记录。
2. 安装完整依赖与 `pytest`，运行 `python -m pytest tests -q`，并运行 `python app.py --self-check`。
3. 在 Windows 环境安装 PyInstaller，分别运行 `python build.py` 和 `python build_cli.py`。两个脚本会覆盖各自的同名产物；先检查需保留的文件。
4. 验证 `dist/tb-verify-ui.exe` 的 `--self-check` 和 `dist/tb-verify-cli.exe` 的 `--help`；检查产物及其校验值。源码测试不能代替打包产物验证。
5. 获得发布授权后，在确认的目标仓库创建版本标签和 Release，上传两个经验证的产物，并回读目标版本和附件。

`tools/` 中的历史发布脚本仍包含旧仓库和旧版本的固定值，不适用于合并后的发布流程。
