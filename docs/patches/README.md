# 车端串口录音补丁（tianracer_core）

这个目录放的是**车端源码**的补丁，不是本仓库的代码。

## 为什么需要它

`probes/serial_tap.py` 的代理桥接能采到原始帧，但它是**控制链路上的单点**：
桥接进程占着真串口，底盘节点读写 pty。桥接一死，链路就断，而两边都不会
立刻发现——上位机以为指令发了，下位机以为对方没说话。停车要靠 MCU 固件
的通信超时保护（已实测 0.4~2.1 秒，但属固件特性）。

改车端源码可以让录音变成**真正的旁听**：通信路径一个字节都不变，
录音只是顺手写一份文件。录音坏了、磁盘满了、目录不可写，通信照常。

## 补丁做了什么

`tianracer_core/include/tianboard/serial.h` 加 5 个私有成员；

`tianracer_core/src/serial.cpp` 加 8 处：

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
cd ~/tianracer_ros1_ws/src/tianracer/tianracer_core
git stash          # 先清干净
patch -p1 < tianracer_core-serial-tap.patch
cd ~/tianracer_ros1_ws
source /opt/ros/noetic/setup.bash
catkin_make --only-pkg-with-deps tianracer_core
```

用 `-p1`：补丁的路径带 `a/` `b/` 前缀。先 `patch -p1 --dry-run` 确认能应用。

`--only-pkg-with-deps` 会写入 `CATKIN_WHITELIST_PACKAGES`，之后编译别的包
会静默跳过。用完清掉：

```bash
catkin_make -DCATKIN_WHITELIST_PACKAGES=""
```

## 验证记录（2026-11-07）

用 pty 虚拟串口，`/tmp/tap_test` 直接链接 `serial.cpp`：

| 场景 | 结果 |
| --- | --- |
| 设 `TIANRACER_TAP_DIR` | `to_mcu.raw` 36 B（3 帧 × 12 B）、`from_mcu.raw` 24 B，字节逐一匹配 |
| 不设开关 | 通信正常，无 tap 日志行，不产生任何文件 |
| 目录不可写 | 两路 fd 均为 -1，通信正常，回传照收 |

车端 `catkin_make --only-pkg-with-deps tianracer_core` 编译通过。

补丁重放验证：从原始的 `serial.h` / `serial.cpp` 出发，`patch -p1` 应用后
两个文件的 md5 与车上实际编译用文件**完全一致**：

```
40fba72855bc334fd6ec451d60726b24  include/tianboard/serial.h
c25807cfecffcf69a267f48eb6d835f8  src/serial.cpp
```

**未验证**：真实底盘串口上的长时间采集、运动状态下的录音、
与 9535 帧那份桥接数据的一致性对比。这几项要在底盘 USB 接上后重做。

## 回退

```bash
cd ~/tianracer_ros1_ws/src/tianracer
git checkout -- tianracer_core/src/serial.cpp \
                tianracer_core/include/tianboard/serial.h
catkin_make --only-pkg-with-deps tianracer_core
```

补丁只做新增：不删代码、不改任何已有语句，所以回退不会丢东西。
