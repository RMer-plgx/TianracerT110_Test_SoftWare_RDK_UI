"""构建入口的产物隔离与自检错误处理。"""

import sys
from types import SimpleNamespace

import pytest

import build
import build_cli


@pytest.mark.parametrize("module,other_name", [
    (build, "ProdTest_CLI"),
    (build_cli, "ProdTest_UI"),
])
@pytest.mark.parametrize("smoke_code", [0, 1])
def test_build_keeps_other_artifact_and_reports_smoke_failure(
    module, other_name, smoke_code, monkeypatch, tmp_path,
):
    dist = tmp_path / "dist"
    dist.mkdir()
    suffix = ".exe" if sys.platform == "win32" else ""
    other = dist / (other_name + suffix)
    other.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setitem(sys.modules, "PyInstaller", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "PyQt5", SimpleNamespace())

    def fake_run(args, **kwargs):
        if "PyInstaller" in args:
            assert "--workpath" in args
            (dist / (module.APP_NAME + suffix)).write_text("built", encoding="utf-8")
            return SimpleNamespace(returncode=0)
        if "pytest" in args:
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=smoke_code, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.main() == (0 if smoke_code == 0 else 1)
    assert other.read_text(encoding="utf-8") == "existing"
