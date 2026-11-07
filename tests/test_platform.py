"""平台抽象层的契约测试。

这些测试只验证「接口形状」与「委托关系」，不连接车辆。
真实采集能力由既有的 check 测试覆盖；本文件的目的是防止：

1. 新增抽象方法后某个实现漏写（ABC 会在实例化时报，但显式断言更早暴露）
2. `file_state` 把「传输失败」和「文件不存在」混为一谈
3. 未知平台名静默回退到默认实现
"""

from __future__ import annotations

import inspect

import pytest

from core.platform import (BasePlatform, Ros1Platform, UnsupportedPlatform,
                           platform_for, registry)


class _FakeResult:
    def __init__(self, stdout="", stderr=""):
        self.stdout = stdout
        self.stderr = stderr


class _FakeRemote:
    """记录收到的命令，按预设返回。"""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.commands = []

    def run(self, cmd, timeout=30, record=True):
        self.commands.append(cmd)
        if not self.replies:
            return _FakeResult("")
        return self.replies.pop(0)


class _FakeCtx:
    def __init__(self, replies=None):
        self.remote = _FakeRemote(replies)
        self.profile = None


class TestAbstractionIsReal:
    def test_base_platform_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            BasePlatform()  # type: ignore[abstract]

    def test_every_declared_capability_is_implemented(self):
        """抽象里声明的方法，实现里都得有。"""
        declared = {n for n in dir(BasePlatform)
                    if not n.startswith("_") and callable(getattr(BasePlatform, n))}
        for impl in (Ros1Platform, UnsupportedPlatform):
            missing = [n for n in declared if not callable(getattr(impl, n, None))]
            assert not missing, f"{impl.__name__} 缺方法：{missing}"

    def test_capability_names_do_not_leak_transport(self):
        """能力名不该出现 ros/ssh/scp 这类传输词。

        命名一旦绑传输，换 ROS2 就得改接口名，抽象就白做了。
        """
        declared = {n for n in dir(BasePlatform)
                    if not n.startswith("_") and callable(getattr(BasePlatform, n))}
        for name in declared:
            low = name.lower()
            for word in ("ros", "ssh", "scp", "sftp"):
                assert word not in low, (
                    f"能力名 {name!r} 含传输词 {word!r}；"
                    f"应按能力命名（如 measure_topic_hz），换平台时接口才稳定"
                )


class TestFileStateFourStates:
    """四态必须分开。「传输失败」当成「不存在」会把采集故障藏成 SKIP。"""

    @pytest.mark.parametrize("reply,expected", [
        ("missing\n", "missing"),
        ("empty\n", "empty"),
        ("nonempty\n", "ok"),
    ])
    def test_known_states(self, reply, expected):
        ctx = _FakeCtx([_FakeResult(reply)])
        assert Ros1Platform(ctx).file_state("/tmp/x.raw") == expected

    def test_command_failure_is_error_not_missing(self):
        ctx = _FakeCtx([_FakeResult("")])       # SSH 没回话
        assert Ros1Platform(ctx).file_state("/tmp/x.raw") == "error"

    def test_unexpected_output_is_error(self):
        ctx = _FakeCtx([_FakeResult("bash: not found\n")])
        assert Ros1Platform(ctx).file_state("/tmp/x.raw") == "error"

    def test_exception_is_error(self):
        class Boom:
            def run(self, *a, **k):
                raise RuntimeError("ssh down")
        ctx = _FakeCtx()
        ctx.remote = Boom()
        assert Ros1Platform(ctx).file_state("/tmp/x.raw") == "error"

    def test_path_is_quoted(self):
        """路径带空格不能被 shell 拆成多个参数。"""
        ctx = _FakeCtx([_FakeResult("missing\n")])
        Ros1Platform(ctx).file_state("/tmp/my tap/from_mcu.raw")
        assert "'/tmp/my tap/from_mcu.raw'" in ctx.remote.commands[0]


class TestRegistry:
    def test_ros1_registered(self):
        assert "ros1" in registry()

    def test_unknown_platform_raises(self):
        """配置写错要立刻暴露，不能静默回退。"""
        with pytest.raises(ValueError, match="未知平台类型"):
            platform_for(_FakeCtx(), "ros9")

    def test_ros1_returns_implementation(self):
        assert isinstance(platform_for(_FakeCtx(), "ros1"), Ros1Platform)


class TestUnsupportedDegradesQuietly:
    """缺平台能力应表现为「未验证」，不该抛异常拖垮整轮检测。"""

    def test_returns_none_not_raise(self):
        p = UnsupportedPlatform("no ros2")
        assert p.measure_topic_hz("/x") is None
        assert p.echo_once("/x") is None
        assert p.run_probe("imu_probe") is None
        assert p.read_bytes("/tmp/x") is None
        assert p.file_state("/tmp/x") == "error"
        assert p.environment_prefix() == ""


class TestRunProbeDelegates:
    def test_delegates_to_util(self, monkeypatch):
        """平台层不重新实现探针分发，只转发。"""
        import checks._util as util
        seen = {}

        def fake(ctx, name, *args, timeout=90, needs_ros=True):
            seen["name"] = name
            seen["args"] = args
            seen["timeout"] = timeout
            return {"ok": True}

        monkeypatch.setattr(util, "run_probe", fake)
        out = Ros1Platform(_FakeCtx()).run_probe("spin_probe", "0.12", timeout=33)
        assert out == {"ok": True}
        assert seen == {"name": "spin_probe", "args": ("0.12",), "timeout": 33}

    def test_signature_keeps_needs_ros(self):
        """`needs_ros` 要透传：静态项不需要 ROS 环境，省一次 source。"""
        sig = inspect.signature(BasePlatform.run_probe)
        assert "needs_ros" in sig.parameters
