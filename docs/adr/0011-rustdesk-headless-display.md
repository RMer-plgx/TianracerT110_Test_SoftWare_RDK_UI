# ADR-0011. RustDesk 黑屏的根因是虚拟显示服务启动失败，由检测软件自动处理

## 状态

Accepted（2026-11-13）。待实施。

## 背景

第 3 项一直报「仅 --service 在运行，远程连接会黑屏」。
用户和之前的假设都认为是**缺 HDMI 诱骗器**，做法是提示用户插拔诱骗器。

**实测证明这个假设只对了一半。**

### 关键证据（192.168.1.93，2026-11-13）

开机 15:08，检查时：

```
lightdm                        inactive
/tmp/.X11-unix/                （空）      ← 没有 X 会话
who                            （空）
/sys/class/drm/.../status      unknown
rustdesk 进程                  只有 --service
```

`systemctl --failed` 报出三个失败单元，其中一个是致命的：

```
● headless-display.service     loaded failed failed Virtual Display Server for Headless Mode
● auto_update_miniboot.service loaded failed failed Auto update miniboot
● hobot-wifi.service           loaded failed failed Hobot load wifi driver
```

`headless-display.service` 的定义：

```ini
[Service]
User=sunrise
ExecStartPre=/usr/bin/bash -c "rm -f /home/sunrise/.Xauthority && touch ..."
ExecStartPre=/usr/bin/xauth -f /home/sunrise/.Xauthority add :0 . $(mcookie)
ExecStart=/usr/bin/Xvfb :0 -screen 0 1920x1080x24 -auth /home/sunrise/.Xauthority
```

失败在**第二条 ExecStartPre**：

```
Process: 1206 ExecStartPre=/usr/bin/xauth ... add :0 . $(mcookie) (code=exited, status=1/FAILURE)
```

### 根因：systemd 不做 shell 命令替换

`$(mcookie)` 在 systemd 的 `ExecStartPre=` 里**不会被 shell 展开**，
它被当作字面量传给 `xauth`。实测：

```
$ xauth -f /tmp/t.Xauthority add :0 . '$(mcookie)'
xauth: (argv):1:  key contains odd number of or non-hex characters
                                          ^^^^^^^^^^^^^^^^^^^^^^^^
$ echo $?
1
```

**同一个命令在 bash 里跑就成功**（`exit=0`），因为 bash 会展开 `$(mcookie)`。

第一条 ExecStartPre 用了 `bash -c` 包裹，所以它能成功；第二条漏了 —— 这就是 bug。

### 连锁反应

```
headless-display 失败
  → Xvfb 没起
  → 没有 :0
  → rustdesk --server 起不来
  → 远程连上是黑屏
```

而 `lightdm` 是 `indirect` 激活，开机不自动启。手动触发它之后：

```
3 秒内：
  /tmp/.X11-unix/      X0
  who                  sunrise tty7 (:0)
  HDMI status          connected / enabled
  xrandr               HDMI-1 connected 1920x1080
  rustdesk             --service + --server + --tray
```

**所以这台车根本不缺诱骗器**：X 一起，`xorg.conf.d/20-virtual.conf` 里的
`Virtual 1920 1080` 就让显示报 connected。

### 用户的实际经验也要解释

用户说「插拔诱骗器就有画面了」。这可能是因为：

- 插拔 HDMI 触发内核热插拔 → 若 X 已在跑，会重新探测显示器 → 有画面
- 但没有 X 时，插拔诱骗器不会有任何作用

**结论**：诱骗器是**必要条件之一**（有些板子没有它 X 起不来），
但不是**充分条件**。真正的缺口是「没有显示服务器在跑」。

## 决策

**分层处理，优先自动修复，绝不要求用户输入命令。**

### 判定逻辑（四态）

| 状态 | 判定 | 摘要 |
| --- | --- | --- |
| 服务未运行 | FAIL | 「RustDesk 服务未启动」 |
| 无 `:0` 图形会话 | **尝试自动修复**，失败则 WARN | 见下 |
| 有会话但无 `--server` | WARN | 「X 已就绪但 RustDesk 未接管，重启 rustdesk 服务」 |
| 全都正常 | PASS | 「服务与用户会话均正常」 |

### 自动修复（核心，满足「傻瓜式」要求）

检测发现没有 `:0` 时，软件**自己**尝试拉起显示服务器，
不需要用户做任何事：

```
1. 试 systemctl start headless-display      ← 符合原设计意图（Xvfb）
2. 若仍无 :0，试 systemctl start lightdm     ← 实测有效（Xorg + Virtual）
3. 每步之间等 2~3 秒，检查 /tmp/.X11-unix/ 是否出现 X0
4. 成功后重新检查 rustdesk --server 是否出现
5. 仍然失败才报 WARN，文案给物理动作而不是命令：
   「请检查 HDMI 诱骗器是否插紧，插好后本项会自动更新」
```

**为什么顺序是 headless 优先**：`headless-display.service` 是镜像原设计
（用 Xvfb 提供虚拟显示），`lightdm` 是备用。修好前者更符合原意。

**为什么还保留 lightdm 兜底**：实测 headless-display 现在的配置是坏的，
而 lightdm + `20-virtual.conf` 能正常工作。两层保险。

### 实时监测（不弹窗）

- 第 3 项在 `run()` 内部轮询，默认 **15 秒**
- 期间每 2 秒检查一次状态
- 一旦满足 PASS 条件立即返回，不必等满 15 秒
- 每 5 秒通过 `ctx.say()` 打一行状态到日志（用户看得到进展，但不打扰）
- **不弹任何对话框**
- 用户点「停止」时立即退出轮询

### 需要 sudo 怎么办

自动修复要 `sudo systemctl start`。密码已在配置里（`ros`），
与现有「清理使用记录」等操作同源，不新增凭据依赖。

## 替代方案

- **只提示用户插拔诱骗器（用户最初的想法）**：实测证明不够 ——
  X 没起来时插拔无效。会把用户引向错误的排查方向。
- **只让用户手工 `systemctl start lightdm`**：违背「不让用户输入命令」的要求。
- **修好车端 service 文件，检测软件什么都不做**：治本，但已经出厂的车
  需要逐台改；检测软件仍应在运行时自愈。
- **改 `headless-display.service` 的 ExecStartPre 加 `bash -c`**：
  车端修复，见下节。与软件自动修复**不冲突**，建议两边都做。

## 车端建议修复（需单独授权）

```ini
# 现在（错）
ExecStartPre=/usr/bin/xauth -f /home/sunrise/.Xauthority add :0 . $(mcookie)

# 应该（对）
ExecStartPre=/usr/bin/bash -c "/usr/bin/xauth -f /home/sunrise/.Xauthority add :0 . $(/usr/bin/mcookie)"
```

改完 `systemctl daemon-reload && systemctl restart headless-display`。
**这一步不改也能工作**（检测软件会自动启 lightdm），但修好后开机就有 X。

## 影响

- 第 3 项耗时从「瞬间」变成最长 15 秒。**只有检测失败时才会等满**，
  正常车瞬间通过。
- 检测软件现在会执行 `sudo systemctl start`，属于**改变车辆运行时状态**。
  这一点要在报告里记录（`result.add_command()`）。
- 如果某台车 `lightdm` 和 `headless-display` 都起不来，仍会 WARN，
  文案改为「请检查 HDMI 诱骗器是否插紧」。

## 未验证

- **修好 `headless-display.service` 后，Xvfb 路线是否真的能支撑 RustDesk？**
  只验证了 lightdm + Xorg 路线。Xvfb 是纯软件渲染，RustDesk 抓屏
  可能拿不到帧。
- **自动修复在「用户正在使用桌面」时是否有副作用？**
  启 lightdm 会创建新会话，如果已有会话则应该是幂等的。
- 只测了 1 台车。其它车的 `headless-display` 是否同样失败未确认。
