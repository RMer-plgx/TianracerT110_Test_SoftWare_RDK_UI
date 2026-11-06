"""命令行入口与共享检测核心的集成检查，不连接车辆。"""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import run


def test_cli_help_exposes_options_without_connecting():
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, str(root / "run.py"), "--help"],
        cwd=root, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0
    assert "--product" in result.stdout
    assert "--no-motion" in result.stdout


def test_cli_uses_shared_runner_and_disables_motion(monkeypatch, tmp_path):
    captured = {}

    class FakeRunner:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.record_dir = tmp_path
            self._lock = None

        def run(self):
            return SimpleNamespace(
                verdict="测试", failed=[], skipped=[], warned=[],
                aborted_reason=None, skip_ratio=0,
            )

    monkeypatch.setattr(run, "Runner", FakeRunner)
    monkeypatch.setattr(run, "render", lambda report, path: (path / "report.md", path / "report.html"))
    monkeypatch.setattr(run, "ask_yes_no", lambda *args, **kwargs: False)
    monkeypatch.setattr(run, "ask_choice", lambda *args, **kwargs: "a")
    monkeypatch.setattr(run, "ask", lambda prompt, default="": default)
    monkeypatch.setattr(sys, "argv", [
        "run.py", "--host", "127.0.0.1", "--user", "test", "--password", "test",
        "--mode", "attach", "--no-motion",
    ])

    assert run.main() == 0
    assert captured["tool_name"] == "prodtest-cli"
    assert captured["mode"] == "attach"
    assert captured["motion"] == "none"
    assert "motion_armed" not in captured["answers"]
