"""更新 GitHub Release 的 exe 附件。

用法：python tools/update_release.py <token>

做两件事：
1. 删除 Release 上的旧附件
2. 上传本地新 exe

只用标准库 urllib，不引入额外依赖。
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

OWNER = "RMer-plgx"
REPO = "TianracerT110_Test_SoftWare_RDK_UI"
RELEASE_ID = 430490975
EXE = Path(__file__).resolve().parent.parent / "dist" / "TianracerT110_Test_SoftWare_RDK_UI.exe"


def api(method, url, token, data=None, headers=None):
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if headers:
        for key, value in headers.items():
            req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
            return resp.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python tools/update_release.py <github-token>")
        return 1
    token = sys.argv[1]

    if not EXE.exists():
        print(f"找不到 exe: {EXE}")
        return 1
    size = EXE.stat().st_size
    print(f"本地 exe: {size} 字节")

    # 1. 列出并删除旧附件
    status, release = api(
        "GET",
        f"https://api.github.com/repos/{OWNER}/{REPO}/releases/{RELEASE_ID}",
        token,
    )
    if status != 200:
        print(f"读取 Release 失败: {status} {release}")
        return 1

    for asset in release.get("assets", []):
        if asset["name"] != EXE.name:
            print(f"跳过其它附件: {asset['name']}")
            continue
        print(f"删除旧附件: {asset['name']} ({asset['size']} 字节, id={asset['id']})")
        code, _ = api(
            "DELETE",
            f"https://api.github.com/repos/{OWNER}/{REPO}/releases/assets/{asset['id']}",
            token,
        )
        if code not in (204, 200):
            print(f"删除失败: {code}")
            return 1

    # 2. 上传新附件
    upload_url = (
        release.get("upload_url", "").split("{")[0]
        or f"https://uploads.github.com/repos/{OWNER}/{REPO}/releases/{RELEASE_ID}/assets"
    )
    print(f"上传中: {EXE.name} …")
    with open(EXE, "rb") as handle:
        payload = handle.read()
    code, result = api(
        "POST",
        f"{upload_url}?name={EXE.name}",
        token,
        data=payload,
        headers={"Content-Type": "application/octet-stream"},
    )
    if code not in (200, 201):
        print(f"上传失败: {code}")
        print(str(result)[:600])
        return 1

    print(f"✅ 上传成功: {result['name']} ({result['size']} 字节)")
    print(f"   下载地址: {result['browser_download_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
