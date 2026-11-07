# 产品插件接口 v1

本文是[总体技术方案](mygo-plugin-platform.md)的接口设计基线，尚无对应 SDK 或可运行实现。字段、调用和状态用于指导 P0/P1 实现与契约测试；P1 通过后冻结 v1.0，之后按兼容规则演进。代码块均为协议示例，不能作为当前仓库的运行命令或已实现 API。

## 插件包与兼容性

一个插件包可声明同一产品系列的多个型号。每轮选择一个 `productId` 和一份基线。产品配置决定要检查什么，插件代码负责计划生成和产品判定，宿主适配器负责已审核的设备操作。

```text
demo-base/
├── plugin.json
├── main.ts
├── products/demo-base.json
├── baselines/demo-base.json
├── schemas/inputs.json
├── assets/setup.png
├── locales/zh-CN.json
├── helpers/                 # 可选；原生辅助程序及依赖
└── integrity.json
```

`plugin.json` 示例使用虚拟产品，相关基线只用于替身测试：

```json
{
  "schemaVersion": 1,
  "id": "org.tianbot.demo-base",
  "name": "示例底盘检测",
  "version": "0.1.0",
  "apiVersion": "1.0",
  "uiSchemaVersion": 1,
  "entry": "main.ts",
  "products": ["products/demo-base.json"],
  "requires": {
    "capabilities": [{"id": "sim.base", "major": 1}],
    "platforms": ["windows/amd64", "darwin/arm64", "linux/amd64"]
  },
  "permissions": {
    "deviceCapabilities": ["sim.base.read"],
    "networkHosts": [],
    "helperIds": []
  },
  "helpers": [],
  "integrity": "integrity.json"
}
```

| 字段 | 约定 |
| --- | --- |
| `schemaVersion` | 包结构版本，未知版本拒绝安装 |
| `id` / `version` | 稳定的反向域名标识和 SemVer 版本；同版本内容不能覆盖 |
| `apiVersion` | 插件 API 主次版本；主版本须一致，运行时次版本须不低于插件要求 |
| `uiSchemaVersion` | 壳必须能渲染的描述模型版本；必需组件不支持时阻止开始 |
| `entry` | 单个 ES module 的 JS/TS 入口；v1 不支持 Node API、运行时 npm 安装或在线 import |
| `products` | 包内产品定义；每项包含产品标识、输入 schema、基线路径、必检项及适用条件 |
| `requires.capabilities` | 所需适配器能力主版本；安装可标缺依赖，开始前必须全部满足 |
| `requires.platforms` | 已交付包可运行的平台；未列出的平台不猜测兼容 |
| `permissions` | 对本轮设备的能力、允许的 HTTP 主机和辅助程序 ID；空列表即不允许 |
| `helpers` | `id/platform/entry/files`，指定平台程序及完整依赖文件；摘要统一进入 `integrity.json` |
| `integrity` | 文件路径、大小、SHA-256 清单；包内容摘要由清单的规范化内容产生，清单不包含自身摘要 |

路径均为包内相对路径，拒绝绝对路径、`..`、符号链接、重复归一化路径和大小写冲突。包安装时检查文件数、总展开大小、压缩比例和每文件上限；P0 将具体上限写入安装器测试夹具。辅助程序可以通过运行时预检使用已安装的受管解释器，缺少解释器或库时明确报依赖错误。

能力声明用于兼容检查，权限用于本轮允许执行的操作，两者分别校验。插件依赖声明不会自动安装宿主模块。v1 不解析跨插件的在线依赖，公共实现应在打包时合入，或通过宿主已有的版本化能力提供。

### 产品定义与基线

`products/*.json` 使用下列结构；路径均指向包内资源。基线独立版本化，运行时保存实际内容与摘要，不能仅记录文件名。`applicableWhen` 只比较已验证的非敏感输入和硬件修订，支持 `eq/in/all/any/not`，不接受脚本表达式；缺少比较字段时预检报错，不默认判为不适用。

```ts
interface ProductDefinition {
  id: string;
  title: string;
  deviceKind: string;                     // 如 drive_base、joint_group
  hardwareRevisions: string[];
  inputSchema: string;                    // 包内 JSON Schema 路径
  baselines: Array<{ id: string; revision: string; path: string }>;
  checks: Array<{
    id: string;
    title: string;
    required: boolean;
    applicableWhen?: Json;                // 受限条件树；省略即始终适用
    criterionKey: string;                 // 引用 baseline.criteria 中的规则
  }>;
  requiredSafetyGates: Array<{ id: string; maxAgeMs?: number }>;
  reviewOnWarnings: true;
}

interface Baseline {
  id: string;
  productId: string;
  revision: string;
  criteria: Record<string, Json>;         // 单位、阈值、采样条件或算法参数
}
```

运行时校验产品、基线、检测项和判定规则的引用完整性。规则可包含数值范围、枚举匹配或插件专属算法参数；后者由产品脚本解释，算法代码与输入证据一起固定版本。`requiredSafetyGates` 引用运行时或适配器登记的条件，产品只能增加条件或缩短有效期。条件是否成立由运行时依据证据计算，脚本返回的布尔值不能代替安全状态。

纯配置产品也交付统一导出入口，可由 SDK 从配置生成；不另设一套执行协议。该生成工具在 P1 实现，当前没有可用命令。

## 插件导出函数

所有插件必须实现下列三项，宿主对导出存在性和返回 schema 分别检查。`describe` 和 `plan` 使用不具备网络、设备、helper、凭据访问的上下文；加载、切换产品和预览计划都不能产生设备副作用。

| 导出 | 输入 | 返回 | 语义 |
| --- | --- | --- | --- |
| `describe()` | 无 | `PluginDescriptor` | 产品、交互描述和能力说明，不连接设备 |
| `plan(input)` | `PlanInput` | `Plan` | 根据显式输入和基线生成有限计划；相同输入产生相同计划 |
| `execute(input, host)` | `StepInput`、宿主代理 | `Promise<StepResult>` | 执行一个已批准的步骤，通过宿主报告进度、取证和请求人工输入 |

执行入口由运行时逐步骤调用。插件不能自行调度下一步、整轮重跑或更换设备。v1 没有自动 `resume()`、任意 `cleanup()` 钩子；停止、资源释放与恢复属于适配器和运行时协议，避免依赖失去响应的脚本完成收尾。

核心类型如下。`Json` 只含 JSON 值，所有测量数字必须有限；大整数序号使用十进制字符串，避免 JS 精度损失。

```ts
type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
type Effect = "read" | "session_change" | "persistent_write" | "motion";
type Verdict = "PASS" | "WARN" | "FAIL" | "SKIP" | "PENDING";

interface PluginDescriptor {
  id: string;
  products: Array<{
    id: string;
    title: string;
    inputSchema: Json;
    form: ViewSpec;
    requiredChecks: string[];
  }>;
}

interface PlanInput {
  productId: string;
  inputs: Record<string, Json>;     // 已验证的非敏感输入
  baseline: Baseline;              // 本轮冻结的产品基线
  capabilityVersions: Record<string, string>;
}

interface Plan {
  schemaVersion: 1;
  steps: Step[];
  requiredChecks: string[];
  deliveryGates: Array<{ id: string; checkIds: string[] }>;
}

interface Step {
  id: string;
  checkIds: string[];
  title: string;
  dependsOn: string[];
  effect: Effect;
  resources: string[];
  timeoutMs: number;
  maxAttempts: number;             // effect 非 read 时必须为 1
  requiredCapabilities: string[];
  requiredGates: string[];
  params: Json;
}

interface StepInput {
  runId: string;
  step: Step;
  attempt: number;
  deviceHandle: string;            // 不透明的本轮设备句柄
  previousResults: CheckResult[];
}

interface StepResult {
  schemaVersion: 1;
  checks: CheckResult[];
}

interface CheckResult {
  checkId: string;
  verdict: Verdict;
  reasonCode: string;
  message: string;
  measurements: Array<{
    key: string;
    value: Json;
    unit: string;
    quality: "valid" | "missing" | "invalid";
    observedAt: string;
    artifactIds: string[];
  }>;
  criteria: Json;                  // 本轮判定规则及阈值
  artifactIds: string[];
}
```

`requiredChecks` 不能由脚本删减产品定义中的必检项。运行时校验步骤 ID 唯一、依赖存在且无环、超时有效、资源与能力已声明，以及全部必检项被覆盖。v1 按依赖顺序串行执行步骤，保留资源字段用于防止助手进程或其他入口竞争设备；并行调度以后单独设计。

`describe` 展开包内 schema 供 UI 使用，产品标识、必检项等元数据必须与产品定义一致。`deliveryGates.checkIds` 引用产品检查，不能通过计划删去交付条件；`requiredGates` 引用已登记的安全条件，并叠加产品与适配器的最低要求。依赖步骤执行出错时阻断后续依赖；步骤正常返回 `FAIL` 时按其证据和条件判定能否继续，其余无风险的只读步骤可以继续取证。依赖采样结果的适配逻辑在已声明能力、参数边界和总超时内执行，不能临时增加运动或持久写入。

适用条件在产品定义中显式表达并进入冻结计划。某项不适用时保留 `SKIP` 和条件证据；在本轮适用条件下仍属必检的项目被 `SKIP/PENDING`，整机自动结论不可交付。没有导出函数、缺适配器、采样失败均不能自动转换为“不适用”。

`effect` 不是插件自行降低风险的开关。适配器按实际调用识别副作用，运行时将实际能力需求与计划声明交叉校验；脚本声明 `read` 却调用运动能力时拒绝调用并记录协议错误。

## 宿主能力 API

`host` 由 worker 代理至运行时。调用始终携带运行时创建的会话上下文，脚本无法自选其他 `runId`、设备或权限。除安全停止路径外，步骤过期后拒绝新的宿主调用。

```ts
interface Host {
  progress(event: { message: string; completed?: number; total?: number }): void;
  log(event: { level: "info" | "warn" | "error"; code: string; message: string }): void;
  cancelled(): boolean;
  device: {
    invoke(input: {
      handle: string;
      capability: string;
      operation: string;
      params: Json;
      timeoutMs: number;
    }): Promise<{ value: Json; artifactIds: string[] }>;
  };
  prompt(input: PromptSpec): Promise<{ promptId: string; values: Record<string, Json> }>;
  artifacts: {
    saveJson(input: { name: string; data: Json; derivedFrom: string[] }): Promise<string>;
  };
}
```

`progress` 和 `log` 限速并限制长度；它们不承担权威状态变更。`device.invoke` 的操作参数按适配器 schema 验证，实际超时取步骤剩余预算与调用预算较小者。原始采集证据由适配器交给运行时保存，脚本无法伪装为原始证据；`saveJson` 只写产品派生数据并关联来源，不接受任意文件系统路径。

首版不提供通用 `host.spawn(command,args)`、shell 或原始 SSH 命令接口。SSH 适配器执行已登记并随版本审核的探针；新增探针或命令属于能力实现变更。helper 由运行时按 manifest 的 ID、平台与摘要启动，worker 通过 `device.invoke` 使用其注册能力。只读 HTTP 采集也须通过具备主机/端口约束的能力，避免插件自行绕过设备操作记录。

凭据字段在表单 schema 中标记 `secret`；UI 直接交给运行时并替换为引用，插件只获得设备句柄及非敏感输入。凭据不放入进程命令行、普通插件存储或 IPC 调试日志。运行时异常输出和原始证据也执行脱敏。

### 人工输入和视图描述

```ts
interface ViewSpec {
  schemaVersion: 1;
  blocks: Array<{
    id: string;
    kind: "text" | "form" | "checklist" | "measurements" | "table" | "image" | "timeseries";
    props: Json;
  }>;
}

interface PromptSpec {
  purpose: "observation" | "safety_confirmation";
  title: string;
  body: ViewSpec;
  timeoutMs: number;
}
```

每种 `kind` 都有对应 props schema，P0 提供实际 schema 和正反例。表单只支持文本、数值、枚举、布尔、凭据、目录选择及受管设备选择；条件显示使用受限的字段比较，不执行代码。未知的必需组件导致预检失败，未知的可选说明可降级成明确占位，不能静默隐藏安全信息。

`promptId` 由运行时创建，绑定会话、步骤、计划摘要及设备身份。输入到达后运行时重新验证有效期、取消状态和安全条件；同一确认只消费一次。脚本可以请求现场观察，但运动授权的确认文案与不可省略条件由运行时根据产品安全要求生成，插件不能伪造已经确认的响应。

## IPC 与请求语义

壳↔运行时使用用户专属的 Unix domain socket（macOS/Linux）或 named pipe（Windows）。启动时建立仅本机当前用户可访问的端点，并通过一次性随机握手凭据核对所属实例；凭据不进入报告或普通日志。端点、PID 和会话发现记录不作为设备权限证明。

运行时↔worker/helper 使用受管管道。消息为 UTF-8 JSON Lines，单帧最多 1 MiB；二进制证据经专用流或受管临时文件进入证据存储，只在消息中返回 artifact ID。stderr 是诊断通道，stdout 不混入普通日志。取消与停止使用独立控制通道或独立高优先级 reader，不能排在采样任务的串行队列后面。

握手先协商 `protocolMajor/protocolMinor`、最大帧长和能力版本。主版本不匹配直接拒绝；未知方法返回 `UNSUPPORTED`，未知可选字段可忽略，影响执行或判定的必需字段缺失则拒绝。协议错误不触发自动设备重试。

```json
{
  "protocolMajor": 1,
  "protocolMinor": 0,
  "kind": "request",
  "id": "req-17",
  "method": "runs.cancel",
  "idempotencyKey": "cancel-run-demo-1",
  "params": {"runId": "run-demo-1", "reason": "operator_requested"}
}
```

响应带相同 `id`，且仅含 `result` 或 `error` 之一。错误结构为 `{code,message,retryable,details}`；`details` 只含脱敏结构化数据。`retryable` 仅提示调用方，不能覆盖副作用步骤禁止自动重试的规则。

| 方法 | 主要输入 | 主要输出与约束 |
| --- | --- | --- |
| `plugins.list` | 无 | 已安装版本、平台兼容性、来源、验收状态 |
| `plugins.prepareInstall` | 本地文件选择得到的资源引用 | 检查结果、待安装 ID 与内容摘要，不执行产品代码 |
| `plugins.install` | 待安装 ID、摘要、用户选择的安装模式、幂等键 | 原子登记已检查的不可变内容；生产模式须命中批准清单，开发模式明确标记 |
| `plugins.activate` | 插件 ID、版本、摘要 | 设置下轮默认版本；活动会话不变 |
| `products.describe` | 插件摘要、产品 ID | 输入 schema、交互描述、能力要求 |
| `runs.prepare` | 产品、非敏感输入、凭据引用、报告根目录 | 冻结计划预览、摘要、缺依赖与副作用；不连接设备 |
| `runs.start` | prepare ID、计划摘要、幂等键 | 新 `runId` 与初始快照；先持久创建会话再派发步骤 |
| `runs.snapshot` | `runId` | 当前状态、最近 seq、活动步骤、停止状态 |
| `runs.events` | `runId`、`afterSeq` | 从持久日志重放，然后订阅新事件 |
| `runs.respond` | `runId`、prompt ID、输入、幂等键 | 确认已接收或明确拒绝；过期、跨会话输入无效 |
| `runs.cancel` | `runId`、原因、幂等键 | 返回已接受请求；最终停止结果通过事件和快照提供 |
| `runs.recover` | `runId`、恢复动作、现场确认 | 只允许已登记的查询/停止/收尾，不自动续跑原动作 |
| `reports.open` / `reports.export` | 报告资源引用、目标目录 | 完整性检查、快照及导出结果 |
| `reviews.append` | 报告摘要、check ID、决定、理由、操作者 | 新记录 ID；原结果和自动结论不变 |

安装检查保留独立暂存快照，`install` 只使用已检查的内容；暂存内容改变或过期时拒绝提交并重新检查。`activate` 单独选择下轮默认版本，安装不会改变活动会话。

`prepare` 不长期保存明文凭据，只绑定用户选择的设备标识和连接参数。`start` 先检查输入是否变化，再连接并核验实际设备身份、型号与硬件修订；不匹配时终止预检，重新选择产品和准备计划，不能自动换绑后继续。所有会影响设备的后续调用只接受已核验句柄。

`install`、`start`、`respond`、`recover` 等有副作用请求的幂等键与参数摘要持久保存：相同键和相同输入返回原结果，同键不同输入拒绝。传输断开后调用方先查询状态，不能生成新键盲目重试。

同一会话按序写入事件，事件包含：

```json
{
  "protocolMajor": 1,
  "kind": "event",
  "runId": "run-demo-1",
  "seq": "42",
  "type": "step.state_changed",
  "stepId": "battery",
  "attempt": 1,
  "occurredAt": "2026-11-07T16:00:00+08:00",
  "payload": {"state": "SUCCEEDED"}
}
```

状态与证据事件持久写入后才推送。UI 根据 seq 去重，发现间隙时补读，不能用本地推断补出“成功”。进度只允许在分配 seq 前合并，所有已编号事件均可从日志重放；终态、确认、错误与证据事件不能丢弃。客户端消费过慢时断开订阅并要求从日志续读，不阻塞控制通道。

## 状态与判定

### 会话状态

| 状态 | 含义 |
| --- | --- |
| `CREATED` | 已创建记录、尚未开始设备步骤 |
| `RUNNING` | 正在检测 |
| `WAITING_INPUT` | 等待本轮人工输入，仍受超时与取消约束 |
| `CANCELLING` | 已接收取消，正在停止和清理 |
| `FINALIZING` | 已结束步骤执行，正在归档与确认收尾 |
| `COMPLETED` | 计划结束、收尾和报告完成，不表示产品通过 |
| `CANCELLED` | 本轮取消且安全收尾完成 |
| `FAILED` | 检测系统失败，但已确认安全收尾 |
| `RECOVERY_REQUIRED` | 动作、资源或持久记录存在未解决状态，需恢复处理 |

`RUNNING/WAITING_INPUT` 可转 `CANCELLING` 或 `FINALIZING`；`CANCELLING` 经收尾转 `CANCELLED`，无法确认则转 `RECOVERY_REQUIRED`。`FINALIZING` 可转 `COMPLETED/FAILED/RECOVERY_REQUIRED`。运行时重启发现未终结会话时进入恢复检查，不能直接返回 `RUNNING`。恢复成功后记录原结束意图并归档为 `CANCELLED/FAILED`，新检测使用新会话。

停止状态另外保存 `NOT_REQUIRED/REQUESTED/COMMAND_ACKNOWLEDGED/SAFE_STATE_CONFIRMED/UNCONFIRMED`。`COMMAND_ACKNOWLEDGED` 只说明命令被接收；`SAFE_STATE_CONFIRMED` 必须有适配器定义的状态证据及产品要求的现场确认。UI 永远不把前者显示成“设备已停稳”。

### 步骤、检测结果与自动结论

步骤执行状态为 `QUEUED/RUNNING/WAITING_INPUT/SUCCEEDED/ERROR/TIMED_OUT/CANCELLED/BLOCKED`。`SUCCEEDED` 表示脚本按契约完成并返回有效结果，其中可以包含产品 `FAIL`。前置条件未满足的步骤为 `BLOCKED`，对应项目保留 `PENDING` 和原因。

单项结果沿用 `PASS/WARN/FAIL/SKIP/PENDING`。采样无效、无数据和未执行保留实际质量信息；运行时根据协议错误或执行失败合成 `PENDING`，另记录错误，不能伪造硬件 `FAIL` 或 `PASS`。

整机自动结论由运行时计算：

- 任一实际 FAIL、适用必检项缺失/未完成/无有效证据、强制交付条件不满足、异常结束或安全状态不明：`NOT_ELIGIBLE`。
- 没有上述阻断，但有 WARN 或产品策略声明必须人工复核的非必需异常：`REVIEW_REQUIRED`。
- 适用必检项及交付条件全部有效完成、证据完整且无待复核异常：`ELIGIBLE`。

人工复核只追加记录，不把 `REVIEW_REQUIRED/NOT_ELIGIBLE` 改写成自动 `ELIGIBLE`。产品必检标准和策略随产品包版本固定；开发包和替身包即使全部 PASS，也不得输出生产可交付结论。

## 超时、取消和资源回收契约

运行时为每个步骤分配 lease，绑定会话、设备和截止时间。worker 只能在租约有效期间发起能力请求。取消时先撤销派发资格，再经控制通道请求适配器停止，等待状态证据、清理本轮拥有的资源，最后结束 worker。worker 不响应时可强制终止，但会话仍按设备状态进入收尾或恢复。

适配器遵守如下接口语义，具体消息同样有请求 ID 和截止时间：

| 操作 | 契约 |
| --- | --- |
| `describe` | 报告协议版本、平台、能力、参数 schema、资源范围和停止保障 |
| `connect` | 核验设备身份，创建本轮句柄；凭据只在适配器调用上下文中可用 |
| `invoke` | 校验动作参数与租约；返回测量/证据，不自行判定整机通过 |
| `requestStop` | 在普通任务阻塞时仍能接收；返回请求状态，不冒充设备停稳 |
| `inspectState` | 提供设备状态、采样时刻、停止证据和不确定项 |
| `close` | 释放本轮拥有的资源，明确剩余资源或错误 |

失去运行时心跳后，适配器在租约截止前进入停止策略；运动还必须有设备侧截止保护。P0/P3 依据设备及操作确定并验证心跳、租约和停止预算，不使用一个全局固定数值声称适合所有机器人。没有经过验证的截止配置时拒绝运动。

命令发送前先记录意图，确认后记录结果；若运行时在二者之间崩溃，恢复时视为结果未知。设备不支持幂等键时，不得以重新发送来查询执行结果。只读查询可在登记的重试策略下重复执行。

## 契约验证与升级

P0/P1 建立 JSON Schema、SDK 类型和跨语言夹具的单一版本来源，避免手写三套不一致类型。测试覆盖 manifest、计划、host 请求、进度与终态、结果、视图描述和原生适配器握手。

至少保留以下端到端夹具：纯配置产品、TS 产品、缺必需能力、错误权限、超时后迟到成功、重复 start、重复确认、worker 死循环、helper 无响应、runner 重启、报告写失败、旧插件版本恢复及两类不同协议设备。夹具中没有实车连接或运动。

主版本变更需要明确迁移工具和旧报告读取策略；新增可选能力可在次版本协商。已归档会话不随协议升级重算结论，报告读不完整时保留缺口。测试命令由 P0/P1 实际工程落地后登记到开发指南，本文件不提供尚不存在的命令。
