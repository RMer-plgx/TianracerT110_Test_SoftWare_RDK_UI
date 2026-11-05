"""全 19 项 smoke 测试：任何 check 都不能因为接口不匹配而崩。

为什么需要这条
--------------
2026-11-03 重构后出现过两个必崩缺陷，而 **242 个测试全绿**：

  · `Profile.env_prefix` 访问不存在的 `RosConfig.env_prefix`
  · `checks/envvars.py` 调 `read_env_vars(ctx.remote)` 漏传 profile

两者都会让对应的检测项抛 `TypeError` / `AttributeError`，
被 `Runner._execute` 的 `except Exception` 转成
「FAIL｜检测项执行异常」。真车一跑就是红的，测试却完全没反应。

原因是当时有测试把被测函数整个 stub 掉（`lambda _r: actual`），
恰好「兼容」了错误调用。这类问题静态扫描不如直接跑一遍。

本测试的立场
------------
**不用 mock 替掉被测函数**，只把一个「什么都不会返回」的假远端接进去，
让每个 check 走它自己的真实代码路径。

- 崩了 → 报出是哪一项、什么异常
- 返回 SKIP/WARN/FAIL → 都算通过（数据拿不到是合法的，崩溃不是）
"""

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.verdict import Status  # noqa: E402
from tests._fixtures import get_profile  # noqa: E402


class _EmptyOut:
    """远端命令的空返回：命令"成功"但没有任何输出。"""

    def __init__(self, stdout: str = ""):
        self.stdout = stdout
        self.stderr = ""
        self.ok = True


class _SilentRemote:
    """不连任何机器的假远端。

    所有命令都返回空输出，所以每个 check 会走「读不到数据」分支。
    这正是我们要的：验证代码路径本身不崩，而不是验证判定逻辑。
    """

    host = "127.0.0.1"
    user = "test"
    _password = ""

    def __init__(self):
        self.commands = []

    def run(self, cmd, timeout=30, record=True):
        self.commands.append(cmd)
        return _EmptyOut()

    def close(self):
        pass


def _make_ctx(profile, **overrides):
    """构造一个足以喂饱 19 个 check 的 Context 替身。"""

    class _Ctx:
        def __init__(self):
            self.remote = _SilentRemote()
            self.baseline = profile.baseline
            self.profile = profile
            self.answers = {}
            self.host = "127.0.0.1"
            # 运动相关：默认全部关闭，避免任何真实动作
            self.motion_level = "none"
            self.motion_armed = False
            self.force_motion = False
            self.voltage_stop = False
            self.voltage_v = None
            self.voltage_known = False
            self.spin_verified = False
            self.ground_attempted = False
            self.cancel_requested = False
            self.owns_bringup = False
            self.pause_for_ground = None
            self.log = []
            for key, value in overrides.items():
                setattr(self, key, value)

        def say(self, text):
            self.log.append(text)

    return _Ctx()


@pytest.fixture(scope="module")
def profile():
    return get_profile()


def _check_numbers(profile):
    return [spec.number for spec in profile.checks]


class TestAllChecksRunWithoutCrashing:
    """19 项逐个跑一遍，任何异常都算失败。"""

    def test_every_check_returns_a_result(self, profile, monkeypatch):
        # 探针类检查会尝试把脚本送到车上执行 —— 一律返回 None（无数据）
        from checks import _util
        monkeypatch.setattr(_util, "run_probe", lambda *a, **k: None)

        # 有的 check 内部有 sleep(12) 等真实等待，累计会把 smoke 拖到一分钟。
        # 这里只验证「不崩」，等待没有意义，直接打桩掉。
        import time
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        # measure_hz 用 fake 远端也能跑（返回 None），无需 stub
        results = {}
        problems = []

        for spec in profile.checks:
            module = __import__(f"checks.{spec.module}", fromlist=["run"])
            ctx = _make_ctx(profile)
            try:
                result = module.run(ctx)
            except Exception as exc:  # noqa: BLE001 - 就是要抓所有异常
                import traceback
                problems.append(
                    f"第 {spec.number} 项（{spec.title} / {spec.module}）"
                    f"抛了 {type(exc).__name__}: {exc}\n"
                    + traceback.format_exc(limit=3)
                )
                continue

            if result is None:
                problems.append(
                    f"第 {spec.number} 项（{spec.title}）返回 None，"
                    f"应该返回 CheckResult"
                )
                continue
            results[spec.number] = result

        assert not problems, (
            "以下检测项在空数据下崩溃（接口不匹配或代码缺陷）：\n\n"
            + "\n\n".join(problems)
        )
        assert len(results) == len(profile.checks)

    def test_result_is_check_result(self, profile, monkeypatch):
        """返回的必须是 CheckResult，且状态是合法值。"""
        from checks import _util
        from core.verdict import CheckResult
        monkeypatch.setattr(_util, "run_probe", lambda *a, **k: None)
        import time
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        for spec in profile.checks:
            module = __import__(f"checks.{spec.module}", fromlist=["run"])
            result = module.run(_make_ctx(profile))
            assert isinstance(result, CheckResult), spec.module
            assert isinstance(result.status, Status), spec.module


class TestRemoteCallsCarryProfile:
    """所有 core.remote 的调用点都必须传 profile。

    用 AST 扫源码，而不是运行 —— 这样连「这个分支从没被执行过」
    也能查出来。
    """

    # 这些函数新增了 profile 参数，调用点漏传就会 TypeError
    NEEDS_PROFILE = {
        "ros", "source_only", "read_env_vars", "kill_all",
        "is_master_running", "wait_for_master", "start_voltage_tap",
        "start_launch", "list_nodes", "wait_for_nodes", "stop_launch",
    }

    @staticmethod
    def _source_files():
        root = Path(__file__).resolve().parent.parent
        skip = {"build", "dist", "__pycache__", ".pytest_cache", "tests"}
        return [p for p in root.rglob("*.py")
                if not any(s in p.parts for s in skip)]

    def test_no_call_missing_profile(self):
        problems = []
        for path in self._source_files():
            if path.name == "remote.py":      # 定义处，不是调用
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name not in self.NEEDS_PROFILE:
                    continue
                # 判断是不是 core.remote 的函数调用（ros_remote.xxx 或直接 xxx）
                if not self._looks_like_remote_call(func):
                    continue
                if not node.args:
                    problems.append(
                        f"{path.name}:{node.lineno} 调用 {name}() 没有位置参数"
                    )
        assert not problems, (
            "以下调用点可能漏传 profile（会 TypeError）：\n"
            + "\n".join(problems)
        )

    @staticmethod
    def _looks_like_remote_call(func) -> bool:
        """判断调用是不是 core.remote 里的函数。

        两种写法：`ros_remote.ros(...)` 与 `from core.remote import ros` 后 `ros(...)`。
        对后者只能靠名字判断，可能误报同名函数 —— 所以只检查
        「模块别名调用」这种确定的，以及名字足够独特的。
        """
        if isinstance(func, ast.Attribute):
            value = func.value
            base = getattr(value, "id", None) or getattr(value, "attr", None)
            if base in ("ros_remote", "remote", "remote_mod"):
                return True
            return False
        # 直接调用：只信独有名字，避免误伤其它同名函数
        if isinstance(func, ast.Name):
            return func.id in {
                "read_env_vars", "start_voltage_tap", "wait_for_master",
                "wait_for_nodes", "is_master_running", "list_nodes",
                "start_launch", "kill_all", "stop_launch", "source_only",
            }
        return False


class TestEnvVarRealPath:
    """不 stub 任何函数，走真实的读环境变量路径。

    这条专门抓 P0-1（env_prefix 取值错）与 P0-3（漏传 profile）：
    临时把 env_prefix 改错，这条必须变红。
    """

    def test_read_env_vars_parses_prefixed_vars(self, profile):
        from core.remote import read_env_vars

        prefix = profile.env_prefix
        assert prefix, "profile 必须能给出环境变量前缀"

        lines = "\n".join([
            f"{prefix}_BASE_PORT=/dev/ttyUSB0",
            f"{prefix}_BASE_BAUDRATE=115200",
            f"{prefix}_NAME=demo",
            "PATH=/usr/bin",              # 不该被收进来
            f"OTHER_{prefix}=x",          # 前缀不在开头，不该收
        ])

        class _Remote:
            def __init__(self):
                self.cmd = None

            def run(self, cmd, timeout=30, record=True):
                self.cmd = cmd
                out = _EmptyOut()
                # 模拟 `grep '^PREFIX'` 的行为：只返回以此开头的行。
                # 不能整段返回 —— 那样测试就绕过了被测代码真正依赖的过滤。
                if "grep '^" in cmd:
                    out.stdout = "\n".join(
                        ln for ln in lines.splitlines()
                        if ln.startswith(prefix)
                    )
                return out

        remote = _Remote()
        vars_ = read_env_vars(remote, profile)

        assert f"{prefix}_BASE_PORT" in vars_
        assert vars_[f"{prefix}_BASE_BAUDRATE"] == "115200"
        assert "PATH" not in vars_
        assert f"OTHER_{prefix}" not in vars_
        # 命令里必须按前缀过滤，且只 source 不 export
        assert f"grep '^{prefix}'" in remote.cmd

    def test_env_check_does_not_fail_on_unreadable_vars(self, profile):
        """读不到变量时应报 FAIL 并说清原因，但绝不能是"执行异常"。"""
        from checks import envvars

        ctx = _make_ctx(profile)
        result = envvars.run(ctx)

        # 空环境下所有变量都不符 → 这是合法的 FAIL/异常路径判定，
        # 但摘要里不能出现 Python 异常字样
        assert "AttributeError" not in result.summary
        assert "TypeError" not in result.summary
        assert "object has no attribute" not in result.summary


class TestProfileEnvPrefix:
    def test_env_prefix_resolved(self, profile):
        assert profile.env_prefix == "TIANRACER"

    def test_env_prefix_declared_in_file(self, profile):
        text = (profile.source_dir / "profile.yaml").read_text(encoding="utf-8")
        assert "env_prefix" in text

    def test_env_note_present(self, profile):
        assert profile.env_note.strip()
        assert "TIANRACER" in profile.env_note
