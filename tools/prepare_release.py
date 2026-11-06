#!/usr/bin/env python3
"""检查待发布的 Windows 文件并生成校验清单；不创建标签或发布。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = (("tb-verify-ui.exe", 2), ("tb-verify-cli.exe", 3))


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True,
                                   encoding="utf-8").strip()


def check_pe(path: Path, expected_subsystem: int) -> None:
    with path.open("rb") as f:
        header = f.read(64)
        if len(header) < 64 or header[:2] != b"MZ":
            raise ValueError(f"{path.name}: 不是有效的 Windows PE 文件")
        offset = struct.unpack_from("<I", header, 60)[0]
        if offset < 64 or offset + 94 > path.stat().st_size:
            raise ValueError(f"{path.name}: PE 头不完整")
        f.seek(offset)
        pe = f.read(94)
        if pe[:4] != b"PE\0\0" or struct.unpack_from("<H", pe, 4)[0] != 0x8664:
            raise ValueError(f"{path.name}: 不是 Windows x86-64 PE 文件")
        if struct.unpack_from("<H", pe, 24)[0] != 0x20B:
            raise ValueError(f"{path.name}: 不是 PE32+ 文件")
        if struct.unpack_from("<H", pe, 92)[0] != expected_subsystem:
            raise ValueError(f"{path.name}: GUI/CLI 子系统与文件名不匹配")


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def prepare(root: Path, dist: Path, source_sha: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("--source-sha 必须是完整的 40 位小写提交 SHA")
    if git(root, "rev-parse", "HEAD") != source_sha:
        raise ValueError("--source-sha 与当前 HEAD 不一致")
    if git(root, "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("工作区存在未提交文件，无法建立明确的源码基线")
    source = git(root, "show", f"{source_sha}:core/__init__.py")
    tree = ast.parse(source)
    version = next((node.value.value for node in tree.body
                    if isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "__version__"
                    and isinstance(node.value, ast.Constant)), None)
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("源码版本号不是 MAJOR.MINOR.PATCH")
    changelog = git(root, "show", f"{source_sha}:CHANGELOG.md")
    first = re.search(r"^## \[([\d.]+)\]", changelog, re.M)
    if not first or first.group(1) != version:
        raise ValueError("CHANGELOG 顶部版本与源码版本不一致")
    if not dist.is_dir():
        raise ValueError(f"产物目录不存在: {dist}")
    outputs = [dist / "SHA256SUMS.txt", dist / "release-manifest.json"]
    if any(path.exists() or path.is_symlink() for path in outputs):
        raise ValueError("清单已存在，请先备份或明确移走旧清单")
    assets = []
    for name, subsystem in ASSETS:
        path = dist / name
        if not path.is_file():
            raise ValueError(f"缺少产物: {path}")
        check_pe(path, subsystem)
        assets.append({"name": name, "size_bytes": path.stat().st_size,
                       "sha256": digest(path), "platform": "windows-x86_64",
                       "subsystem": "gui" if subsystem == 2 else "console"})
    manifest = {
        "version": version,
        "declared_source_sha": source_sha,
        "source_provenance": "declared; binary-to-source provenance not independently verified",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "assets": assets,
    }
    checksums = "".join(f"{item['sha256']}  {item['name']}\n" for item in assets)
    with outputs[0].open("x", encoding="utf-8", newline="\n") as f:
        f.write(checksums)
    with outputs[1].open("x", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True, help="构建时的完整源码 SHA")
    parser.add_argument("--dist", type=Path, default=ROOT / "dist", help="存放两个 exe 的目录")
    args = parser.parse_args()
    try:
        manifest = prepare(ROOT, args.dist.resolve(), args.source_sha)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"发布清单生成失败: {exc}", file=sys.stderr)
        return 1
    print(f"已生成 {manifest['version']} 校验清单；源码 SHA 由操作人声明，仍需构建证据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
