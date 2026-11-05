# 发布流程

> 这份文档回答：**改完之后按什么步骤发一个版本**。
> 之前连续 7 轮改造全挂在 `v1.0` 下，不是能力问题，是没定流程。

## 版本号怎么定

语义化版本 `MAJOR.MINOR.PATCH`：

| 情形 | 例子 | 升哪一位 |
|---|---|---|
| 判定语义反转、配置格式变了、报告结构破坏兼容 | 引入多产品架构 | MAJOR |
| 新增检测项、新模式、新输出格式 | 加有线/无线模式 | MINOR |
| 修 bug、改文档、调阈值 | 修命令注入 | PATCH |

**一次审查修复 = 一个 PATCH**。不要攒着。

## 发版五步

```bash
# 1. 改版本号
#    编辑 core/__init__.py 的 __version__

# 2. 写 CHANGELOG
#    在 CHANGELOG.md 顶部加一节，标题格式：## [x.y.z] — YYYY-MM-DD

# 3. 跑测试（必须全绿才继续）
python -m pytest tests -q

# 4. 提交并打 tag
git add -A
git commit -m "release: v{x.y.z} — {一句话}"
git tag -a v{x.y.z} -m "{一句话}"
git push origin main && git push origin v{x.y.z}
git push tianbot main && git push tianbot v{x.y.z}

# 5. 打包并发布
python build.py
python tools/publish_release.py <token>
```

> `publish_release.py` 里的 tag 要跟第 1 步一致。

## 发布前检查

- [ ] `core/__init__.py` 的 `__version__` 与 CHANGELOG 顶部一致（有测试保证）
- [ ] CHANGELOG 写清了「改了什么、为什么改、影响谁」
- [ ] `git tag` 列表里有这个版本
- [ ] 测试全绿（UI 与 CLI 都跑）
- [ ] 打包产物 `--self-check` 通过（能读到 products 配置）
- [ ] Release 附件字节数与本地产物一致

## 两个仓库

| 仓库 | 角色 |
|---|---|
| `tianbot/tianbot_prodtest_ui` | 组织正式版（公开） |
| `tianbot/tianbot_prodtest_cli` | 组织正式版（私有） |
| `RMer-plgx/TianracerT110_*` | 个人备份，可能滞后 |

每次发布要推**两个**远端。

## 什么时候打 tag

| 时机 | 打不打 |
|---|---|
| 每个 review 轮次修完并验证通过 | ✅ 打 PATCH |
| 新增功能 | ✅ 打 MINOR |
| 重构、判定语义变化 | ✅ 打 MAJOR |
| 只改注释/IDE 配置 | ❌ 不打 |
| 半成品、测试没跑 | ❌ 不打 |

## 已发布版本

见 `CHANGELOG.md`。历史 tag 已按提交内容回溯补齐。
