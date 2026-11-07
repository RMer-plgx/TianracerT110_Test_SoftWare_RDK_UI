# 车端串口录音补丁（tianbot_core）

这个目录放的是**车端源码**的补丁，不是本仓库的代码。

## 先搞清楚：底盘代码在哪个仓库

这一点最容易搞错，包括我自己第一次就改错了包。

| 包 | 仓库 | 作用 | 是否在跑 |
| --- | --- | --- | --- |
| `tianbot_core` | `github.com/tianbot/tianbot_core` | 底盘驱动，**真正打开串口的那个** | **是** |
| `tianracer_core` | `gitee.com/tianbot/tianracer` | 同名 `tianracer_core_node` | **否** |

关键证据在 `tianracer_core/launch/tianracer_core.launch`：

```xml
<node pkg="tianbot_core" type="tianbot_core" name="tianracer" output="screen">
  <param name="serial_port" type="string" value="$(arg serial_port)"/>
```

节点名虽然叫 `tianracer`，**可执行文件来自 `tianbot_core` 包**。
`devel/lib/tianbot_core/tianbot_core` 才是有串口 I/O 的那个二进制。
`tianracer_core` 自己也有个 `tianracer_core_node`，但它不在 any launch 里，
属历史遗留。

所以补丁要打到 **`tianbot_core`**。

## 为什么需要它

`probes/serial_tap.py` 的代理桥接能采到原始帧，但它是**控制链路上的单点**：
桥接进程占着真串口，底盘节点读写 pty。桥接一死，链路就断，而两边都不会
立刻发现——上位机以为指令发了，下位机以为对方没说话。停车要靠 MCU 固件
的通信超时保护（已实测 0.4~2.1 秒，但属固件特性）。

改车端源码可以让录音变成**真正的旁听**：通信路径一个字节都不变，
录音只是顺手写一份文件。录音坏了、磁盘满了、目录不可写，通信照常。

## 补丁做了什么

`tianbot_core/include/tianbot_core/serial.h` 加 5 个私有成员；

`tianbot_core/src/serial.cpp` 加 8 处：

| 位置 | 改动 |
| --- | --- |
| include | 加 `<sys/stat.h>`（`mkdir` 用） |
| 文件级 | 新增 `tap_dump()`：写文件，失败只关自己 |
| `Serial::serial_recv()` | `read()` 之后、`recv_cb_()` 之前录下行 |
| `Serial::send()` | `write()` 循环之前录整帧上行 |
| `Serial::open()` 开头 | 把两路 fd 置 -1（防止提前 return 时是垃圾值） |
| `Serial::open()` 收尾 | 按 `TIANRACER_TAP_DIR` 打开两个日志文件 |
| `Serial::open()` 的 `error:` | 关闭已打开的日志 fd |
| `Serial::close()` | 关闭日志 fd |

## 开关

默认**关闭**。不设环境变量时，每次收发只多一次指针判空。

| 环境变量 | 作用 | 默认 |
| --- | --- | --- |
| `TIANRACER_TAP_DIR` | 录音目录。不设则不录音 | 不设 |
| `TIANRACER_TAP_MB` | 每方向字节上限 | 32 |

录音产物与桥接完全一致，都是**纯原始字节流**，所以
`core/protocol.py` 的 `FrameParser` 与 `checks/protocol.py` 零改动可用：

```
<dir>/to_mcu.raw      上位机 → 下位机
<dir>/from_mcu.raw    下位机 → 上位机
```

用 `O_TRUNC` 打开：一次 bringup 就是一次采集。留着旧字节会让
`checks/protocol.py` 的 `head -c` 取到上一次的数据，把旧数据当本次结果。

## 应用

```bash
cd ~/tianracer_ros1_ws/src/tianbot_core
git stash          # 先清干净
patch -p1 --dry-run < tianbot_core-serial-tap.patch
patch -p1 < tianbot_core-serial-tap.patch
cd ~/tianracer_ros1_ws
source /opt/ros/noetic/setup.bash
catkin_make --only-pkg-with-deps tianbot_core
```

`--only-pkg-with-deps` 会写入 `CATKIN_WHITELIST_PACKAGES`，之后编译别的包
会静默跳过。用完清掉：

```bash
catkin_make -DCATKIN_WHITELIST_PACKAGES=""
```

## 验证记录

### 虚拟串口（pty）

| 场景 | 结果 |
| --- | --- |
| 设 `TIANRACER_TAP_DIR` | 双向字节逐一匹配 |
| 不设开关 | 通信正常，无 tap 输出，不产生任何文件 |
| 目录不可写 | 两路 fd 均为 -1，通信正常，回传照收 |

### 真实底盘串口（2026-11-07）

`/dev/ttyUSB0`（CP2102，115200 8N1）就是底盘 MCU，实测 7 KB/s。
**注意：雷达不占串口**——它是 richbeam LakiBeam1，走网口 192.168.198.2。
`/dev/tianbot_lidar -> ttyUSB0` 是历史遗留的错误软链，richbeam 分支不走它。

**直读对照**（pyserial，不经过节点）：

| 项 | 值 |
| --- | --- |
| 20 秒字节 | 141082 |
| 可解析帧 | 3003 |
| bcc 错 / 长度错 | **0 / 0** |
| 帧型 | ODOM 2001、IMU 1000、Voltage 2 |
| 速率 | 7054 B/s、150 帧/s |

**节点录音**（`roslaunch tianracer_core tianracer_core.launch`，
实际起的是 `tianbot_core` 包的节点，开 `TIANRACER_TAP_DIR`）：

| 项 | from_mcu.raw | to_mcu.raw |
| --- | --- | --- |
| 字节 | 136137 | 864 |
| 帧 | 2898 | 96 |
| bcc 错 / 长度错 | **0 / 0** | **0 / 0** |
| 校验失败帧 | 0 | 0 |
| 帧型 | ODOM 1930、IMU 966、Voltage 2 | HEART_BEAT 96 |

两个方向都在增长（12 s → 22 s：306→756、49458→120110），节点全程存活，
日志零 ERROR、零 `open failed`。录音字节用项目自己的 `FrameParser` 解析，
零错误。

**未验**：运动状态下的录音、与桥接那 9535 帧数据的一致性对比、
分钟级长时间录音的上限截断行为。

## 回退

```bash
cd ~/tianracer_ros1_ws/src/tianbot_core
git checkout -- src/serial.cpp include/tianbot_core/serial.h
cd ~/tianracer_ros1_ws
catkin_make --only-pkg-with-deps tianbot_core
```

补丁只做新增：不删代码、不改任何已有语句，所以回退不会丢东西。
