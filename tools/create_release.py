"""创建 GitHub Release 并上传 exe。

用法：python tools/create_release.py <token>

只用标准库 urllib，不引入额外依赖。
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

OWNER = "RMer-plgx"
REPO = "TianracerT110_Test_SoftWare_RDK_UI"
TAG = "v1.0"
EXE = (Path(__file__).resolve().parent.parent
       / "dist" / "TianracerT110_Test_SoftWare_RDK_UI.exe")

BODY = """## 功能

输入车辆 IP，图形界面完成 19 项整车检测，报告自动归档。

- 三栏布局：左侧配置 / 中间 19 项实时状态卡片 / 右侧运行日志
- 深色、浅色双主题（右上角切换），深色与 logo 金棕色同色系
- 卡片按状态着色：通过绿 / 告警黄 / 失败红 / 跳过灰
- 报告生成后可一键查看（Markdown + HTML + raw.log）

## 使用

下载 `TianracerT110_Test_SoftWare_RDK_UI.exe`，双击运行。
**不需要安装 Python 或任何依赖。**

报告生成在 exe 同目录的 `test records\\` 中。

## 安全说明

- 运动测试**默认全部关闭**
- 「完整运动测试」自动包含微脉冲（约 1.5 cm），两者不是并列选项
- 点击「停止」会**立即用独立连接发送零速**，车马上停，
  不等当前检测项跑完
- 运动中关闭窗口会先停车、清理 ROS；180 秒未结束则拒绝关窗，
  避免出现「窗口没了但车还在动」

## 已知限制

- 停止后当前那一段会自然跑完（最多 3 秒，即该段时长）
- 只支持单台车，批量功能未实现
- 建图质量、导航到点、遥控手感需人工验证
- 仅提供 Windows 版

## 与命令行版的关系

这份是**独立工程**，`core/`、`checks/`、`probes/` 从命令行版复制而来，
之后各自演进。修 bug 需要两边都改。
"""


def api(method, url, token, data=None, headers=None):
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if headers:
        for key, value in headers.items():
            req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = resp.read()
            return resp.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python tools/create_release.py <github-token>")
        return 1
    token = sys.argv[1]

    if not EXE.exists():
        print(f"找不到 exe: {EXE}")
        return 1
    size = EXE.stat().st_size
    print(f"本地 exe: {size} 字节 ({size / 1024 / 1024:.1f} MB)")

    # 1. 创建（或复用）Release
    payload = json.dumps({
        "tag_name": TAG,
        "name": f"{TAG} — Tianracer T110 整车测试（界面版）",
        "body": BODY,
        "draft": False,
        "prerelease": False,
    }).encode("utf-8")

    code, result = api(
        "POST",
        f"https://api.github.com/repos/{OWNER}/{REPO}/releases",
        token,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    if code not in (200, 201):
        print(f"创建 Release 失败: {code}")
        print(str(result)[:500])
        return 1

    release_id = result["id"]
    upload_url = result["upload_url"].split("{")[0]
    print(f"Release 已创建: id={release_id}")

    # 2. 上传附件
    print(f"上传中: {EXE.name} …")
    with open(EXE, "rb") as handle:
        blob = handle.read()

    code, asset = api(
        "POST",
        f"{upload_url}?name={EXE.name}",
        token,
        data=blob,
        headers={"Content-Type": "application/octet-stream"},
    )
    if code not in (200, 201):
        print(f"上传失败: {code}")
        print(str(asset)[:500])
        return 1

    print(f"✅ 上传成功: {asset['name']} ({asset['size']} 字节)")
    print(f"   下载地址: {asset['browser_download_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
