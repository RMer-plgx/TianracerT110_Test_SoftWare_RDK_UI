# tb-verify

车辆出厂检测工具。通过图形界面或命令行连接车载上位机，按产品配置检查网络、系统、传感器和运动控制，并生成检测报告。两个入口共用检测逻辑、产品配置和报告模板。

当前默认产品是 Tianracer T110 RDK，包含 19 项检测。检测清单和产品目标见[产品说明](docs/product.md)。

后续方向已确定为 **MyGo 桌面壳 + 独立检测运行时 + 产品插件**，支持不同协议和流程的机器人，架构及 UI/UX 参考 `mygo-music`。见[完整技术方案](docs/design/mygo-plugin-platform.md)与 [ADR-0006](docs/adr/0006-mygo-plugin-platform.md)。目前只完成方案文档，新平台尚未实现；下文仍描述当前 Python 程序。

![tb-verify 界面](docs/screenshot_dark.png)

## 开始使用

准备一台能通过 SSH 连接车辆的电脑，以及车辆的 SSH 地址和账号。运行检测需要车端具备相应产品配置所要求的 ROS 环境、设备和话题。

从源码运行图形界面：

```bash
python -m pip install -r requirements.txt
python app.py
```

只使用命令行时：

```bash
python -m pip install -r requirements-cli.txt
python run.py
```

命令行会交互询问连接信息和测试条件；可运行 `python run.py --help` 查看参数，使用 `--no-motion` 跳过所有运动测试。详细用法见[命令行说明](docs/CLI.md)。

已有的历史 Windows GUI 包见 [GitHub Releases](https://github.com/tianbot/tb-verify/releases)。合并后的 GUI 和 CLI 版本尚未发布，构建和发布步骤见[发布流程](RELEASE.md)。

## 连接与检测

图形界面提供两种连接方式：

- **无线**：车辆已接入网络时，填写车辆 IP 和 SSH 账号。
- **有线**：通过 USB-C 连接车载上位机，固定地址为 `192.168.128.10`。程序尝试让车辆连接电脑所在的 Wi-Fi，获取无线 IP 后提示拔线，再通过无线连接开始检测。此过程可能在车端创建或更新持久 Wi-Fi 配置。

检测可选择由程序自动启停 ROS bringup，或使用车上已运行的 ROS 环境。自动模式会操作车端 ROS 进程；界面的「清理使用记录」会删除车端日志等文件，执行前需要确认。

## 运动安全

运动测试默认关闭。允许微动后，程序会执行短距离响应检测；图形界面的完整运动测试包含微动、架空和落地阶段，命令行也可选择仅做架空段。落地航向互校会让车辆左右各转一圈，需要约 **2.5 m × 2.5 m** 的开阔平地。开始前确认人员和障碍物已离开测试区域，并准备好遥控器随时接管。

程序会要求确认安全条件，并在电压未知或低于安全阈值时默认跳过运动。CLI 提供显式覆盖选项；使用者须理解其风险并承担相应责任。运行中可请求停止；若车辆未停止或停车指令失败，应立即通过遥控器接管。

## 报告与判定

每次检测生成独立目录，通常包含：

```text
test records/<车辆地址>_<时间戳>/
├── report.html           # 交互式报告，可导出 PDF
├── report.md             # Markdown 报告
├── raw.log               # 已记录的远程命令和输出
├── jspdf.umd.min.js      # 离线 PDF 依赖
└── html2canvas.min.js
```

源码运行时，`test records/` 位于项目目录的上一级；打包运行时位于 exe 同目录。报告生成失败时，CLI 会尝试写入简要的 `report.txt`。

检测项使用 PASS、WARN、FAIL、SKIP、PENDING 等状态。程序根据失败项、未验证项及中止情况给出自动判定；人工复核与自动判定分开展示，不能覆盖原始结果。HTML 报告支持确认告警和未验证项、填写人工检查，并导出 PDF。

当前有几项限制需要结合报告判断：

- **未做落地航向互校时，程序可能仍显示「可交付，但需确认」**。已确认的业务要求是未互校自动判不可交付，这一差距尚未修复；具体见[航向互校决策](docs/adr/0002-ground-heading-verification.md)。
- CLI 退出码目前不能完整表达自动交付结论，不能仅凭退出码 `0` 判断车辆合格。
- 人工勾选状态保存在本机浏览器；操作记录刷新后丢失，不会自动随报告目录转移。
- `raw.log` 不包含所有采集路径；例如电压可能来自未写入命令历史的 tap 文件。证据缺口见[复核与证据方案](docs/adr/0003-review-and-evidence.md)。

## 产品配置

`products/<产品id>/profile.yaml` 定义产品、ROS 环境和检测项，`baseline.yaml` 定义判定阈值。新增产品可从 `products/_template/` 开始，按车型填写配置；已有检测能力可以复用，新设备或检测能力可能需要修改代码。配置缺少必需字段或检测阶段顺序不合法时，加载会报错。

## 开发与验证

```bash
python -m pip install -r requirements.txt pytest
python -m pytest tests -q
```

无显示环境运行界面测试时设置 `QT_QPA_PLATFORM=offscreen`。测试使用替身，不代表实车验证。GUI 自检可运行 `python app.py --self-check`；打包产物仍需单独检查。

| 路径 | 内容 |
| --- | --- |
| `app.py` / `run.py` | 图形界面和命令行入口 |
| `ui/` / `core/` | 界面、连接、执行、判定和报告流程 |
| `checks/` / `probes/` | 检测项及车端探针 |
| `products/` / `report/` | 产品配置及报告模板 |
| `tests/` | 自动化测试 |

更多信息：[产品说明](docs/product.md) · [架构与 ADR](docs/adr/README.md) · [开发与验证](docs/development.md) · [发布流程](RELEASE.md)
