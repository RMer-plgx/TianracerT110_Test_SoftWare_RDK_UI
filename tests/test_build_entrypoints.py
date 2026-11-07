"""图形界面打包的产物隔离与自检错误处理。"""

import sys
from types import SimpleNamespace

import pytest

import build


@pytest.mark.parametrize("smoke_code", [0, 1])
def test_build_preserves_unrelated_dist_files_and_reports_smoke_failure(
    smoke_code, monkeypatch, tmp_path,
):
    dist = tmp_path / "dist"
    dist.mkdir()
    suffix = ".exe" if sys.platform == "win32" else ""

    # dist 里放一个与打包无关的文件，模拟用户自己的东西
    bystander = dist / "README-keep-me.txt"
    bystander.write_text("existing", encoding="utf-8")

    monkeypatch.setattr(build, "ROOT", tmp_path)
    monkeypatch.setitem(sys.modules, "PyInstaller", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "PyQt5", SimpleNamespace())

    workpaths = []

    def fake_run(args, **kwargs):
        if "PyInstaller" in args:
            assert "--workpath" in args, "必须指定 --workpath，否则会污染默认 build/"
            workpaths.append(args[args.index("--workpath") + 1])
            (dist / (build.APP_NAME + suffix)).write_text("built", encoding="utf-8")
            return SimpleNamespace(returncode=0)
        if "pytest" in args:
            return SimpleNamespace(returncode=0)
        # --self-check
        return SimpleNamespace(returncode=smoke_code, stdout="", stderr="")

    monkeypatch.setattr(build.subprocess, "run", fake_run)

    assert build.main() == (0 if smoke_code == 0 else 1)
    assert bystander.read_text(encoding="utf-8") == "existing", (
        "打包不能删掉 dist 里与产物无关的文件")
    assert workpaths == [str(tmp_path / "build" / "ui")]
