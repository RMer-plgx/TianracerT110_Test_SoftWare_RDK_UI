"""发布清单仅验证文件与声明的源码基线，不声称证明构建来源。"""

import hashlib
import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from tools.prepare_release import prepare


def _git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def _pe(path, subsystem):
    data = bytearray(256)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 128)
    data[128:132] = b"PE\0\0"
    struct.pack_into("<H", data, 132, 0x8664)
    struct.pack_into("<H", data, 152, 0x20B)
    struct.pack_into("<H", data, 220, subsystem)
    path.write_bytes(data)


@pytest.fixture
def release_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "core").mkdir()
    (root / "core/__init__.py").write_text('__version__ = "2.3.0"\n', encoding="utf-8")
    (root / ".gitignore").write_text("dist/\n", encoding="utf-8")
    _git(root, "add", "core/__init__.py", ".gitignore")
    _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "test: fixture")
    dist = root / "dist"
    dist.mkdir()
    _pe(dist / "tb-verify-ui.exe", 2)
    return root, dist, _git(root, "rev-parse", "HEAD")


def test_generates_checksums_and_declared_source(release_repo):
    root, dist, sha = release_repo
    stale = dist / "tb-verify-cli.exe"
    _pe(stale, 3)
    result = prepare(root, dist, sha)
    assert [item["name"] for item in result["assets"]] == ["tb-verify-ui.exe"]
    assert stale.is_file()
    assert result["declared_source_sha"] == sha
    assert result["version"] == "2.3.0"
    assert "not independently verified" in result["source_provenance"]
    for item in result["assets"]:
        content = (dist / item["name"]).read_bytes()
        assert item["size_bytes"] == len(content)
        assert item["sha256"] == hashlib.sha256(content).hexdigest()
        assert item["sha256"] in (dist / "SHA256SUMS.txt").read_text(encoding="utf-8")
    assert json.loads((dist / "release-manifest.json").read_text(encoding="utf-8")) == result


def test_rejects_wrong_source_or_dirty_checkout(release_repo):
    root, dist, sha = release_repo
    with pytest.raises(ValueError, match="HEAD"):
        prepare(root, dist, "0" * 40)
    (root / "untracked.txt").write_text("modified", encoding="utf-8")
    with pytest.raises(ValueError, match="未提交"):
        prepare(root, dist, sha)
    assert not (dist / "SHA256SUMS.txt").exists()


def test_rejects_invalid_or_missing_artifacts(release_repo):
    root, dist, sha = release_repo
    _pe(dist / "tb-verify-ui.exe", 3)
    with pytest.raises(ValueError, match="子系统"):
        prepare(root, dist, sha)
    (dist / "tb-verify-ui.exe").unlink()
    with pytest.raises(ValueError, match="缺少产物"):
        prepare(root, dist, sha)
    assert not (dist / "release-manifest.json").exists()


def test_does_not_overwrite_existing_manifest(release_repo):
    root, dist, sha = release_repo
    existing = dist / "release-manifest.json"
    existing.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="清单已存在"):
        prepare(root, dist, sha)
    assert existing.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("name", ["create_release.py", "update_release.py", "publish_release.py"])
def test_legacy_release_commands_are_disabled(name):
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, str(root / "tools" / name), "example-token"],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode != 0
    assert "example-token" not in result.stdout + result.stderr
    assert "RELEASE.md" in result.stderr
