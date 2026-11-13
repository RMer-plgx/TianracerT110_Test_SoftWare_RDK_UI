# ADR-0010. 主控序列号改读 RDK X5 device-tree，并作为平台确认判据

## 状态

Accepted（2026-11-13）。待实施。

## 背景

第 6 项「主控硬件 ID」现在跑的是：

```bash
udevadm info -q property -n /dev/ttyUSB0 | grep ID_SERIAL_SHORT
```

`/dev/ttyUSB0` 是 **CP2102 USB 转串口芯片**。所以 `ID_SERIAL_SHORT`
是这块芯片的 USB 描述符里的序列号，不是任何「主控」的序列号。

### 三个对象，代码读错了

| 对象 | 序列号来源 | 实测值 |
| --- | --- | --- |
| **RDK X5 主控** | `/proc/device-tree/serial-number` | `328150681214c98a00d868f80012004` |
| **CP2102 转串口芯片** | `udevadm ... ID_SERIAL_SHORT` | `0001` |
| 下位机 MCU | 协议里没有序列号字段 | — |

代码读的是第二行，标题写的却是第一行的意思。

### 为什么全是 0001

CP2102 出厂时厂家**可以**烧唯一 ID，也可以不烧。不烧就用默认值 `0001`。
同一批模块全是 `0001` —— 用户测了好几台都是 0001，正是这个原因。

**这个字段无法识别车辆。** 比对它只会制造假告警。

### 平台是 RDK X5，不是 Jetson

本文初版按 Jetson 写。用户 2026-11-13 指出当前主控是 **RDK X5**
（D-Robotics，SoC 为 X5U）。

## 硬件事实（2026-11-13 实测）

| 车 | `/proc/device-tree/model` | `serial-number` |
| --- | --- | --- |
| 192.168.1.82 | `D-Robotics X5 MD V0.2` | `308118a90a2dc19403bb44d80000024` |
| 192.168.1.93 | `D-Robotics X5 MD V1.2` | `328150681214c98a00d868f80012004` |

**两个序列号不同，确认唯一。** 型号版本不同（V0.2 / V1.2），说明批次有差异，
但都能读到序列号。

同机的其它标识（供交叉参考）：

```
/proc/device-tree/compatible   D-Robotics, x5
/sys/class/socinfo/soc_id      X5U
/sys/class/socinfo/board_id    506
/etc/machine-id                93376d30162346569ffd698a59b4b039   ← 镜像克隆后重复
```

`machine-id` 在镜像克隆后会重复，不能当车辆标识；device-tree 的
`serial-number` 才是硬件级的、每台唯一的。

## 决策

**第 6 项改成「平台确认 + 序列号读取」，两者任一不满足都判 FAIL 并中止。**

| 项 | 现状 | 新 |
| --- | --- | --- |
| 标题 | 主控硬件 ID | **主控平台与序列号** |
| 平台判据 | 无 | `model` 含 `D-Robotics X5`，或 `compatible` 含 `D-Robotics, x5` |
| 序列号 | CP2102 `ID_SERIAL_SHORT` | **RDK X5 `/proc/device-tree/serial-number`** |
| 判定 | 与 `expected_serial` 比 | **平台不符 → FAIL 中止；读不到序列号 → FAIL 中止；都对 → PASS** |
| CP2102 | 混在其中 | 降为独立一行，只验证「是 CP2102」（VID:PID），不记序列号 |

### 用户明确要求的语义

> 确认是 RDK 平台，能读到序列号就 pass，如果不是 RDK X5 或者读不到序列号就报错结束

```python
model  = read("/proc/device-tree/model")           # D-Robotics X5 MD V1.2
compat = read("/proc/device-tree/compatible")      # D-Robotics, x5
serial = read("/proc/device-tree/serial-number")   # 328150681214c98a00...

is_x5 = ("D-Robotics X5" in model) or ("D-Robotics, x5" in compat)

if not is_x5:
    FAIL  "不是 RDK X5 平台：{model}"
elif not serial.strip():
    FAIL  "平台正确但读不到序列号（device-tree 无 serial-number）"
else:
    PASS  "{model}，序列号 {serial}"
```

**「报错结束」的含义**：这一项设为 `fatal`，判 FAIL 后中止后续检测。
理由：如果主控换成了别的平台，后面所有阈值（频率、电压、相机分辨率、
轴距轮距）都可能是错的，继续测没有意义，而且会给出误导性的 PASS。

### 为什么保留 CP2102 检查

`VID:PID = 10c4:ea60` 能验证「底盘是通过 CP2102 连接的」——
这是硬件拓扑的一部分，值得记录。但**不记录它的序列号**，
因为没有区分度。

## 替代方案

- **按用户初版提议「标准全改成 0001」**：让 WARN 消失，但这一项从此没有意义。
  既不能识别车辆，也不能验证批次，只是一个恒真的装饰。
- **只记录不判定（本文旧版的方案）**：需要跨车查重才有意义，
  实现复杂且要扫历史报告。用户已明确要求「读不到就报错」，
  直接判 PASS/FAIL 更简单、更严格。
- **读下位机 MCU 序列号**：协议里没有这个字段，改不了。

## 影响

- **报告里的序列号会变。** 从 `0001` 变成 32 位十六进制字符串。
  如果下游有系统按旧格式解析，会失效。
- 第 6 项从 `WARN 可接受` 变成 **`FAIL 即中止`**。
  平台不符或读不到序列号时，检测会在第 6 项就停下。
- `baseline.yaml` 的 `hardware.expected_serial` / `acceptable_serials`
  两个字段废弃，改成 `hardware.required_platform_keywords`（字符串列表）。

## 未验证

- **只验了 2 台车**（V0.2 和 V1.2 各一台）。其它批次是否都能读到
  `serial-number` 未知。如果有批次读不到，会全部 FAIL —— 这是有意的严格，
  但上线前应该至少验 5 台。
- **`/proc/device-tree/model` 的字符串格式**在不同镜像版本上是否稳定？
  现在用「包含」匹配而不是全等，能容忍 `V0.2` / `V1.2` 这类后缀。
- 旧版 ADR 里提到的 `machine-id` 会重复，本方案不依赖它。
