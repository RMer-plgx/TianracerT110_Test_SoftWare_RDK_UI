# 发布流程

tb-verify 的界面版与命令行版使用同一版本号，分别提供 Windows x86-64 产物。当前 `2.3.0` 尚未发布。发布、创建或移动标签、上传附件和公开 Release 需要对应授权；CI 当前未启用。

## 1. 确定源码和版本

- 确认目标仓库 `tianbot/tb-verify`、版本号、发布渠道、负责人及授权；不要根据本地环境猜测目标。
- 更新 `core/__init__.py` 和 `CHANGELOG.md`，移除待发布条目上的“未发布”标记。提交后记录完整 40 位源码 SHA；构建前确认 `HEAD` 与该 SHA 一致、工作区干净。
- tag 使用 `vMAJOR.MINOR.PATCH`，指向同一源码提交。已有 tag 指向其他提交时停止，不移动或覆盖它。

## 2. 验证和构建

在 Windows x86-64 构建环境记录 Windows、Python、PyInstaller 版本及构建命令。安装 `requirements.txt`、`pytest` 和 PyInstaller，运行完整测试 `python -m pytest tests -q`、`python app.py --self-check` 和 `python run.py --help`。测试用替身，不连接实车。

分别运行 `python build.py` 和 `python build_cli.py`。两者会覆盖各自的同名产物；先检查需保留的文件。检查并记录：

| 产物 | 要求 |
| --- | --- |
| `dist/tb-verify-ui.exe` | Windows x86-64；产物自身 `--self-check` 通过 |
| `dist/tb-verify-cli.exe` | Windows x86-64；产物自身 `--help` 通过；检查配置、模板、探针及动态检测模块是否打入包 |

源码自检和 CLI `--help` 不能代替完整产物检查；必要时在干净 Windows 环境验证启动、资源加载和报告输出。没有签名、实车验证或独立复核时如实标明，不把自动测试结果写成对应验收。

## 3. 生成清单并准备草稿

在同一个干净源码 checkout 中执行（PowerShell）：

```powershell
$sourceSha = git rev-parse HEAD
python tools/prepare_release.py --source-sha $sourceSha
```

脚本检查源码 SHA、工作区、版本与 CHANGELOG，确认两个文件的基本 PE 类型并生成 `dist/SHA256SUMS.txt` 和 `dist/release-manifest.json`；若清单已存在则停止，避免覆盖。清单中的源码 SHA 是**操作人声明**，不能单独证明二进制由该提交构建。应另留构建环境、依赖版本、测试与产物检查记录，并核对完整下载的大小和 SHA-256。

按 [Release 模板](.github/RELEASE_TEMPLATE.md)准备说明，写清变化、适用平台、下载文件、校验方式、验证结果和限制。先创建 Draft 并上传两个 exe、`SHA256SUMS.txt`、`release-manifest.json`；不要在命令行参数中传递 GitHub token。避免覆写既有版本或删除已有附件。

## 4. 发布与回读

核对 Draft 的仓库、tag 指向的提交、版本、附件名称、大小、SHA-256 和说明；重新下载附件验证完整性。用户授权公开后发布，并回读 Release 状态、Latest 标记、tag、附件和下载地址。源码 SHA、构建结果和发布后回读分别记录。

`tools/create_release.py`、`tools/update_release.py`、`tools/publish_release.py` 是已停用的旧入口；它们曾固定旧仓库、版本或 Release ID，不能用于当前发布。
