# ADR-0013. 软链冲突修车端 udev 规则，不改检测判据

## 状态

Accepted（2026-11-13）。车端改动待授权。

## 背景

第 5 项报「1 个软链指向可疑，建议清理」。实测拓扑（192.168.1.93）：

```
/dev/ttyUSB0              存在（CP2102, 10c4:ea60，物理口 1-1.1）
/dev/tianbot_lidar -> ttyUSB0      ← 指向底盘串口
/dev/tianbot_base                  ← 不存在
```

### 两条 udev 规则，两条都错

**雷达规则**（`99-tianbot-lidar.rules`）：

```bash
SUBSYSTEM=="tty", SUBSYSTEMS=="usb", DRIVERS=="usb", \
  ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", \
  SYMLINK+="tianbot_lidar"
```

`10c4:ea60` 是 **CP2102 通用 ID**。底盘用的也是 CP2102，所以这条规则
把底盘命名成了「雷达」。

**底盘规则**（`99-tianbot-racecar.rules`）：

```bash
KERNEL=="ttyACM[0-9]*", ... SYMLINK+="/dev/tianbot_base"
```

要求 `ttyACM`，但这台车的底盘枚举成 `ttyUSB0`，所以永远不匹配。

### 关键事实：雷达走网口，不占串口

```
richbeam_lidar.launch.xml:
  <arg name="lidar_ip"   default="192.168.198.2" />
  <arg name="port"       value="2368" />
  <arg name="lidar_serial" default="/dev/ttyUSB0" />   ← 只定义，node 里没引用
```

雷达通过以太网 `192.168.198.2:2368` 通信。**`tianbot_lidar` 这个软链
没有任何程序在读。**

实测确认零引用：

```bash
$ grep -rn 'tianbot_lidar\|tianbot_base' ~/tianracer_ros1_ws/src/*/*/launch/ 
（无输出）
$ grep -c 'TIANRACER_LIDAR_PORT' ~/.bashrc
0
```

底盘则通过 `TIANRACER_BASE_PORT=/dev/ttyUSB0` 直接开串口，
也不经过 `tianbot_base`。

## 决策

**修车端 udev 规则，不改检测判据。**

检测判据「软链指向底盘串口即可疑」是**正确的** —— 它准确识别出了错配。
改判据去接受它，等于掩盖问题。

### 车端修复（最小、无风险）

```bash
# 删除雷达的串口规则：本车型雷达走网口，不需要串口软链
sudo rm /etc/udev/rules.d/99-tianbot-lidar.rules
sudo udevadm control --reload-rules
sudo udevadm trigger
```

### 为什么删掉是安全的

| 检查项 | 结果 |
| --- | --- |
| 有 launch 引用 `tianbot_lidar` 吗 | 无（零引用） |
| `.bashrc` 设了 `TIANRACER_LIDAR_PORT` 吗 | 没有（`grep -c` = 0） |
| 雷达走什么接口 | 网口 `192.168.198.2:2368` |
| 底盘走什么 | `TIANRACER_BASE_PORT=/dev/ttyUSB0` |
| 删掉后软链还在吗 | 不在 —— 正是我们要的 |

删除后：
- `/dev/tianbot_lidar` 消失 → 第 5 项不再报「指向可疑」
- `/dev/tianbot_base` 仍不存在 → 不影响，因为没人读它
- 底盘、雷达都继续正常工作（都不依赖这两个软链）

### 如果将来真的插串口雷达

需要新增一条**按物理端口**区分的规则，而不是用通用 VID:PID：

```bash
# 底盘固定在 1-1.1 口；串口雷达若插在别的口，用 KERNELS 区分
KERNEL=="ttyUSB[0-9]*", KERNELS=="1-1.1", SYMLINK+="tianbot_base"
KERNEL=="ttyUSB[0-9]*", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", \
  KERNELS!="1-1.1", SYMLINK+="tianbot_lidar"
```

但那需要逐台确认 USB 拓扑，不在本轮范围。

### 检测侧的配套改动

只加一条**信息性**记录，不改判据：

- 如果 `/dev/tianbot_lidar` 存在且指向 `ttyUSB*`，
  在 detail 里写明「该软链由 `99-tianbot-lidar.rules` 创建，
  本车型雷达走网口，可删除该规则」
- 让维修人员知道**怎么修**，而不是只知道「有问题」

## 替代方案

- **改检测判据，把 `tianbot_lidar -> ttyUSB0` 当成正常**：等于让检测
  承认一个错配。下次真出问题时，这一项就失效了。
- **保留规则，改软链名为 `tianbot_base`**：改动更大，且与其它车型的
  规则冲突（那些车型的雷达确实走串口）。
- **改 `99-tianbot-racecar.rules` 加 `ttyUSB` 匹配**：会让底盘和雷达
  抢同一个名字（两条 `SYMLINK+=` 都匹配同一个设备）—— 实测就是当前的
  混乱来源。

## 影响

- 删规则后软链消失，第 5 项转 PASS。
- **对车运行零影响**（已逐条验证零引用）。
- 需要 sudo，且每台车都要做一次。建议随镜像更新一起处理。

## 未验证

- **只验了 192.168.1.93 一台**。其它车是否也装了 `99-tianbot-lidar.rules`
  且雷达同样走网口，未确认。如果某些车确实用串口雷达，
  删规则会让那台车的雷达配置失效（虽然不影响当前 bringup，
  因为它走的是另一条 `richbeam_lidar.launch.xml` 分支）。
- 物理 USB 端口（`1-1.1`）在不同车的 hub 拓扑上是否一致，未验证。
