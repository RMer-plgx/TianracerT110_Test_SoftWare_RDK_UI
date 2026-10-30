# 发布指南 — 打包 exe 与发 GitHub Release

> 照着做就行。假设仓库已在 GitHub 上：`RMer-plgx/TianracerT110_Test_SoftWare_RDK`

---

## 一、打包 exe

在项目目录下：

```powershell
cd "D:\Tianbot\产品测试\TianracerT110_Test_SoftWare_RDK"

# 第一次需要装工具（之后不用）
pip install -r requirements.txt
pip install pyinstaller

# 打包
python build.py
```

`build.py` 会依次做四件事：

1. 跑 114 个单元测试，**不通过就不打包**
2. 清理旧的 `build/` 和 `dist/`
3. 调用 PyInstaller 打包（1–3 分钟）
4. 用 `--help` 做冒烟检查，确认产物能启动

成功后：

```
✅ 打包完成：...\dist\TianracerT110_Test_SoftWare_RDK.exe
   体积：12.3 MB
```

---

## 二、本地试跑（发版前必做）

**双击 `dist\TianracerT110_Test_SoftWare_RDK.exe`**，应该看到：

```
==============================================================
  Tianracer T110（RDK）整车自动测试
==============================================================
  检测项 19 项，报告写入 test records/ 目录
  运动测试默认关闭；如启用，车辆会真实移动
==============================================================
```

先走一遍**不连车**的流程确认交互正常：

- 输入一个不存在的 IP 比如 `127.0.0.1`
- 电调选 `n`，车辆状态选 `a`
- 人工项直接回车

应该得到「无法建立 SSH 连接」的判定，并在 **exe 旁边**生成 `test records\` 目录。

> **重点核对**：`test records` 必须和 exe 在**同一个文件夹**。
> 如果你把 exe 拷到桌面，报告就在桌面。

---

## 三、提交代码

```powershell
git add -A
git commit -m "feat: 发布 v1.0（含 exe 打包）"
git push -u origin main
```

> `.gitignore` 已排除 `dist/`、`build/`、`*.spec`，
> 所以**exe 不会被推上去**——它走 Release 附件。

---

## 四、发 Release（网页操作，不用命令行）

### 1. 打标签

```powershell
git tag -a v1.0 -m "v1.0 首个正式版"
git push origin v1.0
```

### 2. 网页创建 Release

1. 打开 `https://github.com/RMer-plgx/TianracerT110_Test_SoftWare_RDK`
2. 右侧栏点 **Releases** → **Create a new release**
3. **Choose a tag** → 选刚推的 `v1.0`
4. **Release title** 填：`v1.0 — Tianracer T110 整车自动测试工具`
5. **Describe this release** 粘贴下面的内容（按需改）：

```markdown
## 功能

输入车辆 IP，自动完成 19 项整车检测，输出 Markdown + HTML 报告。

- 19 项检测：网络 / 身份 / 远程桌面 / 环境变量 / 设备节点 / 主控 ID /
  源码一致性 / IMU / 雷达 / 雷达以太网 / 摄像头 / 里程计 / TF / 电压 /
  建图链路 / teb 插件 / 系统健康 / 运动控制 / 使用痕迹
- 两段式运动测试（架空 bench + 落地 ground），默认关闭
- 判定看**未验证比例**而非通过率，避免拿不到数据被判为合格

## 使用

下载 `TianracerT110_Test_SoftWare_RDK.exe`，双击运行，按提示输入即可。
**不需要安装 Python 或任何依赖。**

报告生成在 exe 同目录的 `test records\` 中。

## 注意

- 运动测试默认关闭。启用后车辆会真实移动（微脉冲约 1.5 cm，
  完整测试前进约 26 cm）。请在架空或空旷环境使用，遥控器待命。
- 首次运行若被 Windows Defender 拦截，选择"仍要运行"，
  或在杀毒软件中把该 exe 加入信任区（PyInstaller 产物常见现象）。

## 已知限制

- 仅支持单台车，批量功能未实现
- 建图质量、导航到点、遥控手感需人工验证
- 仅提供 Windows 版
```

6. 点 **Attach binaries by dropping them here or selecting them**
7. 选择 `dist\TianracerT110_Test_SoftWare_RDK.exe`
8. 点 **Publish release**

---

## 五、发完之后

### 让别人下载

Release 页面里那个 exe 的链接，可以直接发给同事。他们下载后双击即可，不需要任何环境配置。

### 后续更新版本

改完代码后重复：打包 → 试跑 → 提交 → 打新 tag（`v1.1`）→ 发新 Release。

**不要覆盖已有 Release 的附件**，每次都发新版本，这样出问题能回退到旧版。

---

## 常见问题

**Q: 杀软报毒怎么办？**
PyInstaller 打包的程序被误报很常见。两种处理：
1. 用户在杀软里加信任
2. 有条件的话给 exe 做代码签名（需要付费证书，个人项目一般不折腾）

**Q: exe 体积能小点吗？**
12.3 MB 已经比较紧凑（`paramiko` + `cryptography` 占大头）。
如果实在要压，可以改用 `--onedir` 模式，但那样是一个文件夹而不是单个文件，
分发反而不方便。

**Q: 能打 Linux 版吗？**
不改代码就行：在 Linux 上装 PyInstaller 后 `python build.py` 即可。
但当前只验证过 Windows，需要的话另测。

**Q: 打包后中文乱码？**
不应该发生——程序里所有文件读写都显式用了 UTF-8，
输出流也在入口处做了编码设置。如果遇到，先发 issue 附上截图。

---

## 检查清单

发版前逐项确认：

- [ ] `python -m pytest tests/ -q` 全绿
- [ ] `python build.py` 无报错
- [ ] 双击 exe 能进入交互问答
- [ ] 用 `127.0.0.1` 试跑，报告生成在 **exe 同目录**的 `test records\`
- [ ] 报告的中文正常显示（不是 `?` 或乱码）
- [ ] 代码已提交并推送
- [ ] tag 已推送
- [ ] Release 附件已上传且能下载
