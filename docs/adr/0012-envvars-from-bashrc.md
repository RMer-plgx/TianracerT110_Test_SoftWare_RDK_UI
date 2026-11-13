# ADR-0012. 环境变量以车辆 `.bashrc` 的实际声明为标准

## 状态

Accepted（2026-11-13）。待实施。

## 背景

第 4 项报「5 项与环境期望不符（4 项已临时修正，1 项未修正）」。
之前把它归因为「车端 env-hooks 默认值错」，**只对了一半**。

### 完整根因（192.168.1.93 实测）

车上有两处定义环境变量，互相冲突：

**一、`~/.bashrc` 第 199-205 行（正确）**

```bash
export TIANRACER_NAME=tianracer
export TIANBOT_NAME=tianracer
export TIANRACER_BASE_PORT=/dev/ttyUSB0
export TIANRACER_LIDAR=richbeam_lidar
export TIANRACER_BASE_BAUDRATE=115200
```

**二、env-hooks `99.my_tianracer.sh.em`（错误默认值）**

```bash
: ${TIANRACER_BASE_PORT:=/dev/tianbot_base}   # 这个软链不存在
: ${TIANRACER_BASE_BAUDRATE:=460800}          # 本车是 115200
: ${TIANRACER_LIDAR:=oradar_lidar}            # 本车是 richbeam
```

`: ${VAR:=默认值}` 的语义是「VAR 未设置时才赋值」。所以谁先谁后决定结果。

### 关键：`.bashrc` 有非交互提前返回

`~/.bashrc` 第 18-20 行：

```bash
case $- in
    *i*) ;;
      *) return;;      ← 非交互 shell 在这里就退出了
esac
```

所以：

| 场景 | `.bashrc` 是否执行到第 199 行 | 结果 |
| --- | --- | --- |
| 用户 SSH 交互登录 | ✅ 完整执行 | 正确值 |
| 检测软件非交互 SSH 执行 | ❌ 第 20 行 return | **env-hooks 的错误默认值** |

实测：

```
$ bash -ic 'echo $TIANRACER_BASE_PORT $TIANRACER_LIDAR'    # 交互
/dev/ttyUSB0 richbeam_lidar                                  ← 正确

$ bash -c 'source setup.bash; echo $TIANRACER_BASE_PORT'   # 非交互
/dev/tianbot_base                                            ← 错误
```

**这就是「5 项不符」的完整根因。** 车本身没问题，用户交互登录时一切正常；
是检测软件的读取方式（非交互 shell）触发了 env-hooks 的默认值。

### 为什么车还能跑

检测软件启动 bringup 时用 `ros()` 前缀，里面在 source 之后
`export` 了 `env_overrides` 的正确值，把错误值盖掉了。
属于「每次运行临时修正」，不是「车被修好」。

## 决策

**以车辆 `.bashrc` 里显式声明的值为标准，而不是读 source 之后的环境。**

### 判据

```bash
grep -E '^export (TIANRACER|TIANBOT)[A-Z0-9_]*=' ~/.bashrc
```

解析成 `{变量名: 值}`，与 `baseline.yaml` 的 `envvars` 比对。

| 情况 | 判定 |
| --- | --- |
| `.bashrc` 声明了，且等于期望值 | PASS |
| `.bashrc` 声明了，但不等于期望值 | WARN「车辆声明 X，期望 Y」 |
| `.bashrc` 未声明，但 env-hooks 有默认值 | **只记录，不判 WARN**（车没声明，用的是仓库默认） |
| `.bashrc` 未声明，env-hooks 也没有 | WARN「未定义」 |

**关键区别**：不再拿「source 后的进程环境」当事实来源。
因为那被 env-hooks 污染了，反映的不是「车辆配置」，而是「仓库默认值」。

### 车辆标准值（这台车，2026-11-13 实测）

```yaml
# baseline.yaml 的 envvars 段，以此为准
TIANRACER_NAME: tianracer
TIANBOT_NAME: tianracer
TIANRACER_BASE_PORT: /dev/ttyUSB0
TIANRACER_BASE_BAUDRATE: "115200"
TIANRACER_LIDAR: richbeam_lidar
```

其余变量（`TIANRACER_LIDAR_IP`、`TIANRACER_JOY_MODE`、
`TIANRACER_STEERING_DIRECTION` 等）在 `.bashrc` 里**没有声明**，
由 env-hooks 提供。它们继续记录当前值，但**不作为合格判据**。

### 为什么这样改是对的

用户的原话：

> 咱们不能死按照之前那个文档里说的检测，因为这个文档是从之前 jetson nano
> 的检测文档修改而来的，可能环境变量的相关文档内容没有修改，
> 你就把这个车的环境变量当标准

`.bashrc` 是**这台车实际在用的配置** —— 它能让车跑起来（用户交互登录时）。
按它当标准，第 4 项才反映「这台车配得对不对」，而不是「文档写得对不对」。

### 同时需要更新的参考文档

`Tianracer T110（RDK版本）整车测试说明.md` 与
`docs/product.md` 里的环境变量清单同步改成上表 5 项 +
「其余由 env-hooks 提供」的说明。

## 替代方案

- **把 `env_overrides` 全部删掉，让车用 `.bashrc`**：车确实能跑，
  但检测软件的 SSH 会话是非交互的，`.bashrc` 不执行 —— 会立刻坏掉。
  不可行。
- **检测软件改用 `bash -ic`（交互 shell）**：能拿到正确值，但
  `-i` 会加载所有交互配置、打印 banner、可能卡在提示符上。
  在自动化脚本里不可靠。
- **修车端 `.bashrc`，把 export 移到第 20 行 return 之前**：治本，
  但每台车都要改，且改的是用户 home 目录，风险高于收益。
- **把 env-hooks 的默认值全改对**：改的是 git 仓库（`tianracer_bringup`），
  需要推上游。而且它是多车型共用的，改默认值会误伤其它车型。

## 影响

- 第 4 项不再因为 env-hooks 的默认值报 WARN。
  在正确配置的车上会直接 PASS。
- 报告里会区分「车辆声明值」与「运行时实际值」两列，
  排查时能一眼看出是配置问题还是会话问题。
- `env_overrides` 仍然保留（它保证车能跑），但不再是第 4 项的判据来源。

## 未验证

- **只验了 192.168.1.93 一台车**。其它车的 `.bashrc` 是否都有这 5 条、
  值是否一致，未确认。实施前应该扫一遍所有在售的车。
- 如果某台车的 `.bashrc` 里写的是别的值（比如另一批用 oradar 雷达），
  按本方案会报 WARN —— 这是对的，但需要确认那批车是否应该在
  `baseline.yaml` 里另开一个产品配置。
