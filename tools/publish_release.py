#!/usr/bin/env python3
"""在组织仓库创建 Release 并上传 exe。

用法：python tools/publish_release.py <token> [--repo OWNER/REPO] [--exe PATH]

为什么单独写一个而不是复用 update_release.py：
update_release.py 的前提是 Release 已经存在（写死了 RELEASE_ID）。
组织仓库是新建的、还没有任何 Release，需要先 create。

只用标准库，不引入额外依赖。
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 默认目标：组织正式仓库
DEFAULTS = {
    "ui": {
        "repo": "tianbot/tianbot_prodtest_ui",
        "exe": ROOT / "dist" / "TianracerT110_Test_SoftWare_RDK_UI.exe",
        "tag": "v2.1.1",
        "name": "v2.1.1 — 修实车启动失败，建立版本体系",
        "body": """## 本版重点

### 修复实车发现的三个问题

1. **bringup 完全起不来**
   `_run_runtime_phase` 里 `start_launch()` 残留一次机械替换的错误调用
   （多传 3 个参数），异常被内部 `except` 吞掉，表现为「roslaunch
   根本没执行、日志文件都不生成」。已加静态签名校验测试。

2. **雷达与底盘抢串口**
   `TIANRACER_LIDAR_PORT=/dev/tianbot_lidar` 指向 ttyUSB0，与底盘冲突，
   报 `Device /dev/ttyUSB0 open failed` 并反复重试。移除该覆盖后雷达走网口。

3. **版本体系**
   之前 7 轮改造全挂在 v1.0 下，看不出演进。现已补齐 13 个历史 tag、
   CHANGELOG、RELEASE.md，报告里带 `tool_version`。

## 实车验证

修复后跑完整 19 项：17 项完成（12 PASS / 5 WARN），
2 项失败均为车端配置问题（摄像头软链缺失、串口占用），1 项跳过。

## 已知限制

- 不勾「完整运动测试」时，IMU/里程计停在「待落地验证」，
  整机判**不具备交付条件**（航向精度未验证）
- 有线模式会在车上创建 WiFi 连接配置（含密码，默认自动连接）
""",
    },
    "cli": {
        "repo": "tianbot/tianbot_prodtest_cli",
        # CLI 的 exe 在另一个仓库目录里
        "exe": ROOT.parent / "TianracerT110_Test_SoftWare_RDK" / "dist" \
               / "TianracerT110_Test_SoftWare_RDK.exe",
        "tag": "v2.1.1",
        "name": "v2.1.1 — 修实车启动失败，建立版本体系",
        "body": """## 主要变化

- **多产品架构**：检测项、ROS 话题、阈值全部移入 `products/<产品id>/`，
  加新产品只需复制目录改配置，不改代码
- `--product` 参数可指定产品
- **报告内直接下载 PDF**：点按钮直接得到 .pdf
- **落地转向互校**：左右各转一圈，比对里程计与 IMU 的航向
- **人工确认**：告警项可在报告里逐条勾选接受

## 检测项

默认产品 `tianracer_t110_rdk` 共 19 项。

## 已知限制

- 不勾「完整运动测试」时，IMU/里程计停在「待落地验证」，
  整机判**不具备交付条件**
- CLI 版不含有线/无线自动配置（那是界面版功能）
""",
    },
}


def api(method, url, token, data=None, extra=None):
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if extra:
        for key, value in extra.items():
            req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def upload_asset(repo, release_id, exe, token):
    data = exe.read_bytes()
    url = (f"https://uploads.github.com/repos/{repo}/releases/{release_id}"
           f"/assets?name={urllib.request.quote(exe.name)}")
    return api("POST", url, token, data=data,
               extra={"Content-Type": "application/octet-stream"})


def publish(which, token):
    cfg = DEFAULTS[which]
    repo, exe = cfg["repo"], cfg["exe"]

    if not exe.is_file():
        print(f"❌ 找不到 exe：{exe}")
        return False

    print(f"=== {repo} ===")
    print(f"  本地 exe：{exe.name} {exe.stat().st_size} 字节")

    # 1. 已存在同 tag 的 Release 就复用，否则新建
    status, releases = api("GET", f"https://api.github.com/repos/{repo}/releases",
                           token)
    release = None
    if isinstance(releases, list):
        release = next((r for r in releases if r.get("tag_name") == cfg["tag"]), None)

    if release:
        print(f"  复用已有 Release id={release['id']}")
    else:
        status, release = api(
            "POST", f"https://api.github.com/repos/{repo}/releases", token,
            data=json.dumps({
                "tag_name": cfg["tag"],
                "name": cfg["name"],
                "body": cfg["body"],
                "draft": False,
                "prerelease": False,
            }).encode("utf-8"),
            extra={"Content-Type": "application/json"},
        )
        if status not in (200, 201):
            print(f"❌ 创建 Release 失败：HTTP {status}\n{release}")
            return False
        print(f"  ✅ 创建 Release id={release['id']} tag={cfg['tag']}")

    # 2. 删掉同名旧附件（重复上传会 422）
    for asset in release.get("assets", []):
        if asset["name"] == exe.name:
            api("DELETE",
                f"https://api.github.com/repos/{repo}/releases/assets/{asset['id']}",
                token)
            print(f"  删除旧附件 {asset['name']}")

    # 3. 上传
    print(f"  上传中…（{exe.stat().st_size // 1024 // 1024} MB）")
    status, result = upload_asset(repo, release["id"], exe, token)
    if status not in (200, 201):
        print(f"❌ 上传失败：HTTP {status}\n{str(result)[:400]}")
        return False

    print(f"  ✅ 上传成功 {result['name']} ({result['size']} 字节)")
    print(f"  {result['browser_download_url']}")
    return True


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python tools/publish_release.py <token> [ui|cli]")
        return 1
    token = sys.argv[1]
    which = sys.argv[2] if len(sys.argv) > 2 else None

    targets = [which] if which in DEFAULTS else list(DEFAULTS)
    ok = True
    for name in targets:
        ok = publish(name, token) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
