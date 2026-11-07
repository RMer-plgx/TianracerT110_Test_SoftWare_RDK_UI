# 检测能力平台抽象层

状态：**接口已建立，唯一实现是 ROS1；现有检测项尚未迁移，`core/platform.py`
在生产代码里零引用**（`grep -r "core.platform\|from .platform" core checks app.py`
无命中）。提交信息与模块 docstring 已写明「本轮只建边界」，这里再显式标注一次，
避免读者以为迁移已完成。

本文说明 `core/platform.py` 的设计与迁移路径，服务于
[ADR-0006](../adr/0006-mygo-plugin-platform.md) 的插件化方向。

## 为什么需要这一层

现在 20 个检测项各自直接调 `run_probe()`、`measure_hz()`、`ctx.remote.run()`。
ROS 与 SSH 的细节散落在每个 check 里。两个后果：

1. **换平台要改 20 个文件。** 插件契约
   （[plugin-api-v1.md](plugin-api-v1.md#宿主能力-api)）里的宿主能力是
   `device.invoke({capability, operation, params})` —— 一个**命名能力**，
   不是一条 SSH 命令。check 直接拼命令，这层契约就无法落地。
2. **检测逻辑无法单独测试。** 验证「频率低于阈值判 FAIL」时必须伪造
   SSH 输出，而不是注入一个返回固定值的假平台。

## 接口

`BasePlatform` 按**能力**命名，不按传输方式命名：

| 方法 | 返回 | 失败时 |
| --- | --- | --- |
| `measure_topic_hz(topic, seconds)` | `float \| None` | `None` |
| `echo_once(topic, field, timeout)` | `str \| None` | `None` |
| `run_probe(name, *args, timeout, needs_ros)` | `dict \| None` | `None` |
| `read_bytes(path, max_bytes)` | `bytes \| None` | `None` |
| `file_state(path)` | `missing/empty/ok/error` | `error` |
| `environment_prefix()` | `str` | `""` |

约定：

- **失败返回 `None`，不抛异常。** 检测项要能在缺数据时给出 SKIP/FAIL，
  而不是让整轮检测崩掉。
- **`file_state` 必须四态。** 「传输失败」与「文件不存在」混为一谈，
  会把一次采集故障静默成 SKIP，而 SKIP 不影响交付判定。
- **命名不带传输词。** 有一条测试（`test_capability_names_do_not_leak_transport`）
  强制这一点。写这条测试时它立刻抓到了我自己的 `ros_env_prefix`，
  改名为 `environment_prefix` 后才通过 —— ROS1 与 ROS2 都要 source 环境，
  接口不必跟着发行版变。

## 实现

| 类 | 用途 |
| --- | --- |
| `Ros1Platform` | 唯一实现。委托给 `checks._util` 与 `core.remote`，不重新实现 |
| `UnsupportedPlatform` | 占位。全部返回 `None`/`error`，让检测项自然落到 SKIP |

`platform_for(ctx, kind)` 按名字取实现。**未知类型抛 `ValueError`**，
不静默回退 —— 配置写错要立刻暴露，否则「以为在测 A 平台、实际跑在 B 平台」
这种错误无从发现。

## 与 robot_check 的关系

`robot_check`（`github.com/tianbot/robot_check`，`main` `c739f5d`，2026-01-21）
有一层 `BasePlatform` + `ros1_impl.py` / `ros2_impl.py`，思路相同。
本模块借鉴「按能力命名」，但**不采用它的判定方式**：

| | robot_check | tb-verify |
| --- | --- | --- |
| 判定档位 | PASS / FAIL 两档 | PASS/WARN/FAIL/SKIP/PENDING 五档 |
| 报告 | 文本日志，保留 20 份 | HTML + Markdown + PDF，可人工复核 |
| 检测项定义 | `config/check_params.yaml`，纯数据 | `checks/*.py`，每项一个模块 |
| 安全约束 | 无（不驱动车） | 电压门禁、架空确认、停车与清理 |
| 平台抽象 | 有 | 本轮建立 |

**robot_check 值得吸收的两点**：

1. **配置驱动的被动检查。** 话题、类型、最小频率、抖动上限、`frame_id`
   要求全写在 YAML 里，加一项不用改代码。我们这块写死在 `checks/*.py`。
2. **systemd + 日志轮转。** 出厂检测要「插上就跑」时很实用。

**不宜照搬**：两档判定无法表达「未验证」与「有偏差」，而 ADR-0002 要求
未完成互校即判不可交付 —— 这需要 PENDING 档。

## 迁移路径

本轮只建立边界，**不改任何 check**。后续按 check 逐个迁移：

```python
# 现在
hz = measure_hz(ctx, topic)
# 迁移后
hz = ctx.platform.measure_topic_hz(topic)
```

迁移一个 check 的前置条件：

1. 该 check 的所有采集都走平台方法，没有残留的 `ctx.remote.run` 拼命令
2. 有测试能在假平台上跑通判定分支
3. `raw.log` 里的命令记录不丢失（平台层要能转发 `add_command`）

**未做**：`ctx.platform` 还没挂到 ctx 上。挂上去需要改
`core/runner.py` 的 Ctx 构造，影响面比新增一个模块大，留到迁移时一起做。

## 未验证

- ROS2 实现（`ros2_impl`）不存在，也不猜测兼容
- 迁移后对现有 20 项检测行为的影响（本轮零改动，故零影响）
- 平台层是否能覆盖插件契约里 `device.invoke` 的全部语义（需要 P2 落地时验证）
