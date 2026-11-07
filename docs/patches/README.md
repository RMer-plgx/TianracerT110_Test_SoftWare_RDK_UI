# 车端补丁（tianbot_core）

这个目录放的是**车端源码**的补丁，不是本仓库的代码。
两个补丁有先后依赖，必须按编号顺序应用。

## 先搞清楚：底盘代码在哪个仓库

这一点最容易搞错，我自己第一次就改错了包。

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
`devel/lib/tianbot_core/tianbot_core` 才是有串口 I/O 的那个二进制；
`tianracer_core` 自己那个 `tianracer_core_node` 不在任何 launch 里，属历史遗留。

## 两个补丁

| 序号 | 文件 | 作用 | 车端提交 |
| --- | --- | --- | --- |
| 01 | `tianbot_core-01-raw-mode.patch` | 串口改成真正的 raw 模式 | `212785e` |
| 02 | `tianbot_core-02-serial-tap.patch` | 通讯录音（旁听） | `8c502d6` |

### 01 raw mode

`Serial::config()` 里有两处把二进制串口当成终端配置：

```c
options.c_lflag |= ~(ICANON | ECHOE | ECHO | ISIG);   // 应为 &= ~
options.c_cflag |= IXON | IXOFF | IXANY;              // 应在 c_iflag
```

第一行写成 `|=` 而不是 `&= ~`，等于把除这 4 位以外的**所有**标志位置 1，
同时 `icanon/echo/echoe/isig` 仍然保持打开 —— 与意图完全相反。
后果：行缓冲（等换行才返回）、输入回显到输出、`0x03`/`0x1C` 被当作信号字符吞掉。
`0x03` 在二进制帧的数据段里是可能出现的，被吞掉就直接破坏帧校验。

改法：`tcgetattr` 之后立刻 `cfmakeraw()`，一次把输入/输出/本地模式全设为
二进制安全；末尾手写的 `c_oflag` / `c_lflag` 删除（已被覆盖）。
流控改为先清干净再按需打开，软件流控写到 `c_iflag`。

**顺序约束**：`cfmakeraw` 会清掉 `CLOCAL` 和 `CREAD`，必须放在
`|= CLOCAL` / `|= CREAD` 之前。补丁已保证这一点，后续改动不要破坏它。

参照上游 `codex/fix-serial-raw-mode` 分支 `59bd6c8` 的做法；本补丁在该思路下
按本仓库代码结构落地，并补充了顺序约束与验证证据。

### 02 通讯录音

在底盘节点自己的收发路径上顺手把原始字节写一份到文件，供检测软件离线解析。

改动只在两处真实 I/O：

| 位置 | 改动 |
| --- | --- |
| `Serial::serial_recv()` | `read()` 之后、`recv_cb_()` 之前录下行 |
| `Serial::send()` | `write()` 循环之前录整帧上行 |

外加 `tap_dump()` 辅助函数、5 个私有成员、`open()` 初始化与清理、`close()` 清理。

**为什么不用 pty 桥接**：桥接是控制链路单点，它挂掉链路就断，两边都不会立刻
发现；录音模块挂掉只丢录音，控制链路照常。

## 开关

默认**关闭**。不设环境变量时，每次收发只多一次指针判空。

| 环境变量 | 作用 | 默认 |
| --- | --- | --- |
| `TIANBOT_TAP_DIR` | 录音目录。不设则不录音 | 不设 |
| `TIANBOT_TAP_MB` | 每方向字节上限 | 32 |

前缀用 `TIANBOT_` 而不是 `TIANRACER_`：本仓库服务多种车型
（`param/` 下有 t108、tom06、tom08、motec_diff 等），录音是通用能力。

录音产物与桥接完全一致，都是**纯原始字节流**，`core/protocol.py` 的
`FrameParser` 与 `checks/protocol.py` 零改动可用：

```
<dir>/to_mcu.raw      上位机 → 下位机
<dir>/from_mcu.raw    下位机 → 上位机
```

用 `O_TRUNC` 打开：一次 bringup 就是一次采集。

## 应用

```bash
cd ~/tianracer_ros1_ws/src/tianbot_core
git stash
patch -p1 --dry-run < tianbot_core-01-raw-mode.patch
patch -p1 < tianbot_core-01-raw-mode.patch
patch -p1 --dry-run < tianbot_core-02-serial-tap.patch
patch -p1 < tianbot_core-02-serial-tap.patch
cd ~/tianracer_ros1_ws
source /opt/ros/noetic/setup.bash
catkin_make --only-pkg-with-deps tianbot_core
```

用 `-p1`：补丁路径带 `a/` `b/` 前缀。

`--only-pkg-with-deps` 会写入 `CATKIN_WHITELIST_PACKAGES`，之后编译别的包会
静默跳过。用完清掉：`catkin_make -DCATKIN_WHITELIST_PACKAGES=""`。

## 验证记录（2026-11-07，实车 /dev/ttyUSB0）

`/dev/ttyUSB0`（CP2102，115200 8N1）就是底盘 MCU，约 7 KB/s。
**雷达不占串口** —— 它是 richbeam LakiBeam1，走网口 192.168.198.2。
`/dev/tianbot_lidar -> ttyUSB0` 是历史遗留的错误软链，richbeam 分支不走它。

### 01 raw mode

- 编译通过
- `stty -a -F /dev/ttyUSB0`（节点已打开）：
  `-opost -ocrnl -onlcr -isig -icanon -iexten -echo -echoe -extproc`，
  `cread clocal -crtscts -ixon -ixoff`，`cs8 -parenb -cstopb`
- 60 秒运行：BCC error 0、open failed 0

### 02 录音

- 编译通过
- 开 `TIANBOT_TAP_DIR` 跑 25 秒：`from_mcu` 167935 B / 3571 帧、
  `to_mcu` 1049 B / 115 帧，两方向持续增长
- 用项目 `FrameParser` 解析录音：**bcc 错 0、长度错 0、校验失败帧 0**
- 帧型：ODOM 2379 / IMU 1189 / Voltage 2 / DEBUG_RESPONSE 1；
  上行 HEART_BEAT 113 / DEBUG 2
- `BCC error 0`，无 `Communication with base error`
- 不设 `TIANBOT_TAP_DIR`：不产生任何文件，节点照常工作
- 录音目录不可写：两路 fd 均 `-1`，通信照常

### 补丁重放

从 `7ce54f3` 的原始文件出发，按顺序 `patch -p1` 应用两个补丁，
结果与车端 `HEAD` 完全一致：

```
528ce34dbe2fa3b4f638ad709296c748  src/serial.cpp
394c406605d86b929133bb78ee20158b  include/tianbot_core/serial.h
```

## 已知陷阱

**`DEFAULT_SERIAL_BAUDRATE` 是 460800，本车 MCU 是 115200。**

`core.cpp:311` 读 ROS 参数 `serial_baudrate`，取不到才用默认值 460800。
`tianracer_core.launch` 里显式传了 115200，所以正常 bringup 没问题；
但直接 `rosrun tianbot_core tianbot_core _serial_port:=/dev/ttyUSB0`
**漏传 `_serial_baudrate:=115200` 会得到规律性乱码**：

```
78 00 78 f8 f8 f8 00 78 00 f8 f8 78 80 ...
```

这不是录音功能的故障，是波特率不匹配。验收时如果看到这种字节，
先确认波特率，别去怀疑录音代码。

**`roslaunch` 会自动 respawn 被杀掉的节点。**
`pkill -9 -f tianbot_core` 之后 roslaunch 立刻重启它，多个进程交替占用串口，
录音里得到的是被拆散的字节流。清理时必须**先杀 roslaunch，再杀节点**：

```bash
pkill -9 -f 'roslaunch tianracer'; sleep 1
pkill -9 -f tianbot_core; sleep 1
pkill -9 -f rosmaster
```

验收时用 `pgrep -fc tianbot_core` 确认只有 1 个进程，
用 `ls -l /proc/*/fd | grep -c ttyUSB0` 确认只有 1 个 fd。

## 回退

```bash
cd ~/tianracer_ros1_ws/src/tianbot_core
git checkout -- src/serial.cpp include/tianbot_core/serial.h
cd ~/tianracer_ros1_ws
catkin_make --only-pkg-with-deps tianbot_core
```

02 只做新增：不删代码、不改任何已有语句。01 会改写 `config()` 里的三处，
但都是等价替换（把错误的位操作改成正确形式）。

## 未验证

- 运动状态下的录音
- 分钟级长时间录音的上限截断行为
- 与桥接那 9535 帧数据的一致性对比
- 其他车型（t108 / tom06 / tom08 / motec_diff）上的表现
