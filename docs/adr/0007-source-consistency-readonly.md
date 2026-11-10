# ADR-0007. 源码一致性只读比对远端，不在检测过程中改动被检对象

## 状态

Accepted（2026-11-10）。实现于 `checks/source.py`，已在实车跑通。

## 背景

第 7 项「源码一致性」原来的实现只做两件事：读本地 HEAD、数
`git status --short` 的行数。然后给出结论：

```
N 个仓库均与远端一致，无未提交改动
```

**这句话是凭空的。** 它从来没跟远端比过 —— 只要没有未提交改动就说「一致」。

实车立刻露馅。2026-11-10 检测一台车，第 7 项显示一致，而实际上：

```
tianbot_core:  本地 dev 7ce54f3  →  origin/dev c47921d   落后 2 个提交
```

上游那两个提交正是修串口 raw 模式的（`59bd6c8` + `c47921d`，PR #9）。

用户此前反复问「我们是不是一直在用老代码、老镜像」。工具不但没回答这个问题，
还给出了**错误的安心感** —— 这比不给结论更糟。检测工具的价值在于
「说了 PASS 就真的没问题」，一旦出现伪 PASS，整个报告的可信度都要打折。

## 决策

第 7 项改为比对远端，并遵守两条约束：

### 一、只读，不 fetch

用 `git ls-remote --heads <remote> <branch>` 直接问远端分支的 SHA。

**为什么不用 `fetch`。** `fetch` 本身是安全的：只写 `.git/refs/remotes/`，
不动工作区、不动 HEAD、不影响运行中的节点、不需要重新编译。这一点在实车上验证过：

```
fetch 前  tianbot_core HEAD = 8c502d66d751...
fetch 后  tianbot_core HEAD = 8c502d66d751...    ← 没变
          工作区 3 项未提交改动仍在
fetch 后起 bringup：7 节点全部就绪，waited 12.8s
              /tmp/tap/from_mcu.raw  23500 → 59220 B（5 秒内继续增长）
              第 20 项 PASS，下行 3172 帧、bcc 错误 0
```

安全，但仍然不用它。理由是**原则**：检测工具不该在检测过程中改变被测对象的状态。
`fetch` 会改动本地引用，于是同一辆车连测两次，第一次「落后」、第二次可能「同步」——
结果取决于测了几次，而不取决于车是什么状态。这不是检测该有的性质。

`ls-remote` 零副作用。

### 二、如实报告「不知道」

`ls-remote` 的代价是：远端提交若本地从未 fetch 过，本地没有那个对象，
算不出 ahead/behind 的具体条数。这时报告写「落后（本地未 fetch）」，不猜。

同理，`ls-remote` 失败的三种情况必须分开，因为含义完全不同：

| 现象 | 含义 | 本项怎么记 |
| --- | --- | --- |
| 命令返回非 0 | remote 地址坏 / SSH 不通 / 无凭据 | 远端不可达 |
| 命令成功但输出为空 | **远端没有这个分支** | 本地分支未推送 |
| 输出有内容但解析不出 SHA | 无权限或地址异常 | 无法解析 |

第二种是实车上的真实情况：`tianbot_core` 的 `feat/serial-tap-recording`
只存在于这台车上。这种**未推送的本地分支**比「落后」更该被看见 ——
上面的补丁换一台车就没了，出厂前必须知道。

## 由此暴露的车端现状（2026-11-10 实测）

三个仓库指向三个不同的源，此前无人注意：

| 仓库 | remote URL | 实际情况 |
| --- | --- | --- |
| tianracer | `https://gitee.com/tianbot/tianracer.git` | **Gitee**，不是 GitHub |
| tianbot_core | `https:///gh-proxy.com/https://github.com/tianbot/tianbot_core.git` | 三斜杠坏地址 + gh-proxy 镜像 |
| webrtc_ros_bridge | `git@github.com:tianbot/webrtc_ros_bridge.git` | SSH，车上无 host key，报 `Host key verification failed` |

`webrtc_ros_bridge` 因此**从来没能从车上成功 fetch 过**，
它与远端的差距无从得知。

## 替代方案

- **执行 `fetch` 再比**：能给出精确 ahead/behind，但违反「不改被测对象」。
  若要精确数字，应在**检测之外**单独跑一次同步，再让检测读结果。
- **只比提交时间戳**：`git log -1 --date` 无法判断领先/落后，只能看新旧。
  已否决 —— 时间新不代表代码新（rebase 会改时间）。
- **完全交给人工**：现在这条路。它已经失效过一次，不再接受。

## 影响

- 第 7 项从「归档检查」升级为「版本一致性检查」。
- **「本地分支未推送」判 FAIL**（2026-11-10 Round 14 审查后定案）。
  初版判 WARN，理由是「不影响本次检测的其它项」。这个理由站不住：
  本项是**出厂检查**，出厂的东西必须可复现。落后还能 `fetch` 补回来，
  未推送的分支不能 —— 换一台车、换一个维修点就再也拿不到那份改动。
  「滞后 / 超前 / 无法比对」仍判 WARN：它们与车况无关，可人工确认接受。
- 四类问题（未推送 / 不一致 / 无法比对 / 无法读取）**一次性全部列出**，
  不再 early return。旧实现里「A 仓库读不到」会把「B 仓库落后」盖掉。
- 报告里新增「相对远端」一行，带 `比对 origin/xxx @ <sha>（ls-remote，未改动本地）`
  的注脚，方便事后复核这个结论是怎么来的。
- 端到端仍是 `git pull` 的手工活。本项**只报差异，不动手修**。

## Round 14 审查后的四处修正（2026-11-10）

初版重写虽然方向对，但有四个实现缺陷，都已在同一轮修完并在实车复验：

### 1. 三斜杠诊断曾被 `continue` 变成死代码

旧顺序是先 `ls-remote`，失败就 `continue`，而 `git remote get-url` 的检查在
`continue` **之后**。三斜杠地址必然让 `ls-remote` 失败，于是这条唯一的根因
诊断永远打不出来 —— 而本项目的 `tianbot_core` 恰好就是三斜杠。

现在 URL 在 `ls-remote` **之前**取，且失败时在 note 里追加
「地址三斜杠，需先修 remote URL」。实车可见：

```
tianbot_core remote 地址: https:///gh-proxy.com/https://github.com/tianbot/...
                          // 格式异常（三斜杠），ls-remote/fetch 必然失败
```

配套测试原本是**假绿**：构造了「ls-remote 成功 + URL 三斜杠」这种现实中
互斥的组合。已改为「ls-remote 失败 + URL 三斜杠」。

### 2. SSH 故障曾被归因成「落后（本地未 fetch）」

`git cat-file -e` 对「对象不存在」返回非 0，对「SSH 断了」也返回非 0，
两者无法从退出码区分。旧实现据此一律报「本地未 fetch」，维修人员会去
`fetch` 而不是查网络。

现在改用 `if cat-file; then echo HAVE; else echo MISS; fi` 显式标记，
拿不到 HAVE 也拿不到 MISS 就判「读取失败」并归入「无法比对」。
祖先判断同理，显式回传 `RC=`。

### 3. 输出解析曾被 git 的 warning 行污染

`gh-proxy` 镜像会打 `warning: redirecting to ...`，而 `2>&1` 把它并进 stdout。
旧解析只看第一行，于是把「未推送分支」误报成「输出无法解析」。

现在扫描**所有行**找 `<sha>\t<ref>`，warning 不影响判定。
只有输出里出现 `fatal:` / `permission denied` 这类词才当错误处理。

### 4. shell 拼接

路径与分支名全部经 `core.ssh.shquote`：git 的分支名规则允许 `;` `$` `&` `|`
和反引号，裸拼进 shell 就是注入点。

一个容易踩的坑：`shquote('~/ws')` 得到 `'~/ws'`，**单引号里的 `~` 不展开**，
`cd` 会失败。所以 `~/` 前缀单独处理成 `"$HOME"/`，其余段再转义。
另去掉了 `str.format` 拼脚本 —— workspace 路径里有 `{` 会让它抛 `ValueError`。

## 未验证

- `webrtc_ros_bridge` 与远端的实际差距（SSH 不通，无数据）
- 修好 remote 之后，本项在 `webrtc_ros_bridge` 上的行为
- 上游用 `cfmakeraw()` 重写 raw 模式后（`59bd6c8`），
  我们的补丁 02（录音）能否无冲突地叠在上面
