# 当前架构

本文记录 tb-verify 当前的架构和已确认决策。具体背景与取舍见各项 ADR；已决定但尚未实现的内容会标明差距，候选方案不作为现行能力。产品目标和检测清单见[产品说明](../product.md)。

## 已确定的新平台方向

[ADR-0006](0006-mygo-plugin-platform.md) 确认以 MyGo 桌面壳、独立检测运行时和 JS/TS 产品插件构建多机器人检测平台，架构与 UI/UX 参考 `mygo-music`。插件提供产品流程、判定和交互描述，运行时负责会话、设备能力、停止、恢复和证据；原生 SDK 按需经辅助程序接入。

目前[技术方案](../design/mygo-plugin-platform.md)、[插件接口](../design/plugin-api-v1.md)和[UI/UX 设计](../design/mygo-ui-ux.md)已完成；**P0 探针已实现**（`platform/`，2026-11-07），包含包校验、协议 schema、JS 沙箱、一次性子进程探针与原生界面 spike。检测运行时会话、设备能力、证据归档与正式 UI 尚未实现，与现有 Python 程序无集成。下述当前架构仍指 Python 实现；NSH 是独立探索，不构成新平台的成本或实施依赖。

## 入口与产品配置

<!-- ai-lint: ok 正文和表格重复 链接路径与索引重复，便于从相关架构跳转 -->
- Python 检测工具的图形界面入口是 `app.py`；检测核心、报告及产品配置在同一仓库。见[共用核心决策](0001-shared-core-and-product-profiles.md)。
- 产品定义和阈值分别由 `profile.yaml`、`baseline.yaml` 提供；产品配置决定检测项及执行顺序，加载时检查阶段顺序与必需配置。新增能力可能需要代码修改。

## 检测与交付

<!-- ai-lint: ok 正文和表格重复 链接路径与索引重复，便于从相关架构跳转 -->

- 运行数据主要通过 ROS 获取；系统、网络和设备信息还通过 SSH 等方式采集。
  底盘协议帧录制**首选车端内嵌录音**（`docs/patches/`，默认关闭），通讯路径
  不变，录音模块挂掉不影响控制；代理桥接（`probes/serial_tap.py`）降为备选，
  因为它是控制链路的单点。第 20 项在未启用采集时判 SKIP，不影响交付判定。
  车端录音已于 2026-11-07 在实车验证（3571 帧、bcc 错 0）。
  MCU 通信超时保护已实测确认（0.4~2.1 秒内停住车），但属固件特性，
  采集期间仍建议架空。见[协议观测方案](0004-protocol-observation.md)。
- 检测能力正在向**平台抽象**收敛：`core/platform.py` 定义了按能力命名的
  `BasePlatform`（`measure_topic_hz` / `run_probe` / `file_state` 等），
  目前只有 ROS1 实现，**现有检测项尚未迁移**。这层是为 ADR-0006 的插件契约
  `device.invoke` 铺路。见[平台抽象层](../design/platform-abstraction.md)。
- 已确认未完成落地航向互校时自动判不可交付。**2026-11-07 修复了「转圈从未执行」**
  （`_run_ground` 把 `ground` 段当整个 `motion` 传给 `_run_spin`，导致每次都落进
  「配置缺失」分支）；但「未选择运动时 IMU/里程计仍可能 PASS」这一判定差距
  仍未消除，整机仍可能显示可交付但需确认。两者是不同的事。
  见[航向互校决策](0002-ground-heading-verification.md)。
- **转向角读不到，只能间接测。** MCU 无角度回传帧，2026-11-10 用
  「恒定转角 + IMU 角速度反推」完成首次标定：`δ_real = 0.910·δ_cmd + 0.58°`，
  残差 RMS 0.15°。0.35 rad（20.05°）指令实际约 18.8°。
  `HARD_MAX_STEER = 0.5` 是随手定的保守上限，不是实测物理限位。
  见[转向标定](0005-steering-calibration.md)。
- **四驱同速导致转弯必然打滑 29%，回不到原点属正常。**
  T110 用一个电机经齿轮/皮带带四个轮子，转速强制相同；而转弯时
  内外轮需差 `W/R`（δ=0.35 时 22.8%）、前后轴路径差 6.5%（合计 29.3%）。
  实测左转 28.4%、右转 34.5%，与几何理论吻合。
  因此第 18 项的主判据改为**车体姿态误差**（IMU 相对目标，实测 0.7°），
  打滑率与**几何理论值**比对，**闭合误差只记录不判定**（实测 1.1~1.3 m）。
  场地需约 4m×4m（含滑移余量），不是纯圆弧算的 1.8 m。
  见[转向标定](0005-steering-calibration.md)的「追加发现」。
- **打滑率不再否掉「落地互校通过」**（2026-11-13）。实测两台次：姿态误差
  0.42~0.96°（容差 3°）但打滑率 45~52%（理论 30%±15）→ 判 WARN →
  连带 IMU/里程计保持 PENDING。打滑率主要反映地面摩擦，不是车的能力，
  故降为独立 WARN。见[落地互校判据](0008-spin-verified-criteria.md)。
- **检测项的展示顺序与执行顺序分开**（2026-11-13）。执行仍按
  `static → runtime → motion`（硬依赖），报告按编号 1..20 渲染；
  第 20 项前置到 runtime 最前，让协议层数据能被后续项引用。
  见[展示顺序](0009-check-order-display-vs-execution.md)。
- **四个车端问题已有定论，均不改车运行逻辑**（2026-11-13）：
  - 主控已换为 **RDK X5**（不是 Jetson）。序列号改读 device-tree，
    并作为平台确认判据：读不到则 FAIL 中止。见 [ADR-0010](0010-mainboard-serial-from-rdk-x5.md)。
  - RustDesk 黑屏的真因是 `headless-display.service` 启动失败
    （systemd 不展开 `ExecStartPre` 里的 `$(mcookie)`），不是缺诱骗器。
    见 [ADR-0011](0011-rustdesk-headless-display.md)。
  - 第 4 项环境变量：`.bashrc` 第 18-20 行非交互提前 `return`，
    导致检测软件读到 env-hooks 的错误默认值。改以 `.bashrc` 声明为准。
    见 [ADR-0012](0012-envvars-from-bashrc.md)。
  - 第 5 项软链：雷达规则用通用 CP2102 ID，把底盘命名成了雷达。
    删规则即可（零引用，不影响运行）。见 [ADR-0013](0013-symlink-udev-rule-fix.md)。
- **源码一致性原先根本没比远端，2026-11-10 重写。** 旧实现只数本地未提交
  改动，却把结论写成「与远端一致」—— 实车当场露馅（tianbot_core 落后上游
  2 个提交，报告仍说一致）。现用 `git ls-remote` 只读比对，不执行 `fetch`：
  fetch 本身安全（实测不影响 bringup 与采集），但它会改变被测对象的状态，
  同一车连测两次结果会变。**本地分支未推送判 FAIL**（换台车复现不出来），
  滞后/超前/无法比对判 WARN；四类问题一次列全，不 early return。
  **交付镜像自带的车端改动可经 `ros.allowed_local_branches` 声明**，
  声明后降为 WARN（否则装了串口补丁的每台车都永久 FAIL，第 7 项就没人看了）。
  **detached HEAD 归 WARN**：`git checkout <tag>` 核对版本是正常动作，
  不能因此判整车不合格。
  见[源码一致性只读比对](0007-source-consistency-readonly.md)。

- 当前自动判定保留 FAIL、致命项 PENDING 和未验证比例等规则。
- 运动测试前保留电压检查、人员确认、取消、停车与 ROS 清理路径。实车连接、运动及修改车端配置需明确授权。

## 报告与证据

<!-- ai-lint: ok 正文和表格重复 链接路径与索引重复，便于从相关架构跳转 -->
- 每轮输出 HTML、Markdown 和已记录命令的 `raw.log`。HTML 可离线导出 PDF；人工勾选与自动结论分别展示，但当前勾选状态只保存在浏览器，操作日志刷新后丢失。见[复核与证据方案](0003-review-and-evidence.md)。
- **PDF 导出按内容白行切页，不硬切像素。** 把整份报告画成 canvas 再按页高切，
  切点落在文字行中间就会出现「半个字跨页」。CSS 的 `break-inside: avoid` 对此
  **无效** —— html2canvas 不走浏览器打印引擎。现在切页前往回找一条全白横带，
  找不到才退回硬切；同时加 12 mm 页边距（原来全出血，打印机必裁边）。
- 电压可能来自未写入命令历史的 tap 文件，当前不能保证所有结果在 `raw.log` 可追溯。复核记录持久化和证据补齐仍是候选方案。

## 连接与发布

- 有线连接可能在车端创建持久 Wi-Fi 配置；自动运行模式可能启停 ROS，使用痕迹清理会删除车端数据。使用及副作用见[项目说明](../../README.md)与[开发指南](../development.md)。
- Windows 图形界面打包与发布见[发布流程](../../RELEASE.md)。CI 当前未启用。

## 决策记录

| 编号 | 主题 | 决策状态 | 实现状态 |
| --- | --- | --- | --- |
| [0001](0001-shared-core-and-product-profiles.md) | 共用核心与产品配置 | Accepted | 已有实现 |
| [0002](0002-ground-heading-verification.md) | 落地航向互校与自动交付判定 | Accepted | 转圈未执行的缺陷已修复（2026-11-07）；未互校即判不可交付的逻辑仍未实现 |
| [0003](0003-review-and-evidence.md) | 复核记录与证据追溯 | Proposed | 尚未完整实现 |
| [0004](0004-protocol-observation.md) | 底盘协议观测 | Proposed | 车端内嵌录音已实现并在实车验证（3571 帧零错）；代理桥接降为备选；未接入正式流程 |
| [0005](0005-steering-calibration.md) | 转向角间接测量与标定 | Accepted | 标定完成且**判据已按物理约束重写**：打滑率 29% 是四驱同速的几何必然，姿态误差才是主判据 |
| [0006](0006-mygo-plugin-platform.md) | MyGo 桌面壳与多机器人产品插件 | Accepted | P0 探针已实现（包校验/schema/沙箱/探针/界面 spike）；运行时会话、设备能力、正式 UI 未实现 |
| [0007](0007-source-consistency-readonly.md) | 源码一致性只读比对远端 | Accepted | 已实现；未推送分支判 FAIL，交付镜像自带分支可经 `allowed_local_branches` 白名单降为 WARN；detached HEAD 归 WARN |
| [0008](0008-spin-verified-criteria.md) | 落地互校的通过只由姿态误差决定 | Accepted | 待实施：打滑率降为独立 WARN，不再否掉 IMU/里程计 |
| [0009](0009-check-order-display-vs-execution.md) | 展示顺序与执行顺序解耦 | Accepted | 待实施：报告按编号 1..20，执行仍按 static→runtime→motion；第 20 项前置 |
| [0010](0010-mainboard-serial-from-rdk-x5.md) | 主控序列号改读 RDK X5 device-tree | Accepted | 待实施：现在读的是 CP2102 芯片序列号（全批 0001）；改为平台确认 + 序列号，读不到则 FAIL 中止 |
| [0011](0011-rustdesk-headless-display.md) | RustDesk 黑屏根因是虚拟显示服务启动失败 | Accepted | 待实施：`headless-display.service` 的 `$(mcookie)` 未被 systemd 展开；检测软件自动拉起显示服务器 |
| [0012](0012-envvars-from-bashrc.md) | 环境变量以车辆 `.bashrc` 声明为标准 | Accepted | 待实施：`.bashrc` 非交互提前 return，导致读到 env-hooks 的错误默认值 |
| [0013](0013-symlink-udev-rule-fix.md) | 软链冲突修车端 udev 规则 | Accepted | 待实施：雷达规则用通用 CP2102 ID，把底盘命名成了雷达；删规则（零引用） |

记录约定：每项可独立取舍的架构决策使用独立文件，记录状态、背景、决策、替代方案和影响。`Accepted` 表示决策已确认，不代表实现或验收完成；`Proposed` 表示方案待确认。实现变化时同步更新本总览；被替代的决策保留记录并注明后续决策。
