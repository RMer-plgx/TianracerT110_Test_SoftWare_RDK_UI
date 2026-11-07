"""检测能力的平台抽象层。

存在理由
--------
现在 20 个检测项各自直接调 `run_probe()`、`measure_hz()`、`ctx.remote.run()`，
ROS 细节散落在每个 check 里。这带来两个问题：

1. **换平台要改 20 个文件。** ADR-0006 定的方向是「检测运行时 + 可插拔产品
   插件」，插件契约（`docs/design/plugin-api-v1.md`）里的宿主能力是
   `device.invoke({capability, operation, params})` —— 一个命名能力，
   不是一个 SSH 命令。若 check 直接拼命令，这层契约没法落地。

2. **无法测试「检测逻辑」本身。** 想验证「频率低于阈值判 FAIL」时，
   必须伪造 SSH 输出；有了平台接口，可以注入一个返回固定值的假平台。

本模块只做一件事：**把已经存在的调用收敛成命名能力**，不动实现，
不改任何 check。现有行为完全不变，后续按 check 逐个迁移过来。

与 robot_check 的关系
--------------------
`robot_check`（`github.com/tianbot/robot_check`，2026-01-21）有一层
`BasePlatform` + `ros1_impl.py` / `ros2_impl.py`，思路相同。本模块借鉴了
「按能力命名，不按传输命名」这一点，但保留本项目的判定分级
（PASS/WARN/FAIL/SKIP/PENDING）与证据要求，不采用它的 PASS/FAIL 二档。

不做的事
--------
- 不引入新的检测逻辑。所有方法都委托给 `checks._util` 的现有实现。
- 不改 check 的调用方式。本轮只建立边界，迁移是后续工作。
- 不承诺 ROS2 已可用。`Ros1Platform` 是唯一实现，
  ROS2 实现缺失时构造函数明确抛错，不猜测兼容。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class BasePlatform(ABC):
    """检测能力接口。

    每个方法对应一类证据。命名按**能力**而不是按传输方式：
    叫 `measure_topic_hz` 而不是 `rostopic_hz`，叫 `run_probe` 而不是
    `sftp_and_exec`。这样换 ROS2 或换 REST 适配器时接口不变。

    所有方法在失败时返回 `None`，不抛异常 —— 检测项要能在缺数据时
    给出 SKIP/FAIL，而不是让整个检测崩掉。
    """

    # ---- 采集 ----

    @abstractmethod
    def measure_topic_hz(self, topic: str, seconds: int = 5) -> Optional[float]:
        """话题频率（Hz）。取不到返回 None。"""

    @abstractmethod
    def echo_once(self, topic: str, field: str = "",
                  timeout: int = 12) -> Optional[str]:
        """取一条消息的原始文本。`field` 非空时按字段过滤。"""

    @abstractmethod
    def run_probe(self, name: str, *args: str, timeout: int = 90,
                  needs_ros: bool = True) -> Optional[Dict[str, Any]]:
        """在目标机上执行具名探针，返回它输出的 JSON。

        探针源码来自本仓库 `probes/<name>.py`，随检测软件版本一起固定。
        ADR-0006 的插件契约要求「已登记并随版本审核的探针」，
        这里的 `name` 就是登记标识；传任意命令不是本接口的能力。
        """

    # ---- 文件 ----

    @abstractmethod
    def read_bytes(self, path: str, max_bytes: Optional[int] = None) -> Optional[bytes]:
        """读取目标机上的文件原始字节。不存在或读失败返回 None。"""

    @abstractmethod
    def file_state(self, path: str) -> str:
        """文件状态：``missing`` / ``empty`` / ``ok`` / ``error``。

        四态必须分开：把「传输失败」当成「文件不存在」会把一次采集故障
        静默成 SKIP，而 SKIP 不影响交付判定 —— 等于把失败藏起来。
        """

    # ---- 环境 ----

    @abstractmethod
    def environment_prefix(self) -> str:
        """进入运行环境并导出产品变量的 shell 前缀。

        ROS1 下是 source setup.bash 再 export；ROS2 下是 source setup.bash，
        接口不变。
        """


class Ros1Platform(BasePlatform):
    """ROS1 + SSH 的实现。委托给 `checks._util` 与 `core.remote`。

    延迟导入 `checks._util`：那层历史上依赖 `core.remote`，
    顶层互相 import 会成环。延迟到方法调用时再取，代价可忽略。
    """

    def __init__(self, ctx: Any):
        self._ctx = ctx

    # ---- 采集 ----

    def measure_topic_hz(self, topic: str, seconds: int = 5) -> Optional[float]:
        from checks._util import measure_hz
        return measure_hz(self._ctx, topic, seconds)

    def echo_once(self, topic: str, field: str = "",
                  timeout: int = 12) -> Optional[str]:
        from checks._util import echo_once
        return echo_once(self._ctx, topic, field, timeout)

    def run_probe(self, name: str, *args: str, timeout: int = 90,
                  needs_ros: bool = True) -> Optional[Dict[str, Any]]:
        from checks._util import run_probe
        return run_probe(self._ctx, name, *args, timeout=timeout,
                         needs_ros=needs_ros)

    # ---- 文件 ----

    def read_bytes(self, path: str, max_bytes: Optional[int] = None) -> Optional[bytes]:
        import base64
        from core.ssh import shquote

        limit = f"head -c {int(max_bytes)} " if max_bytes else "cat "
        cmd = f"{limit}{shquote(path)} 2>/dev/null | base64 -w0"
        try:
            result = self._ctx.remote.run(cmd, timeout=60, record=False)
        except Exception:
            return None
        text = "".join((result.stdout or "").split())
        if not text:
            return None
        try:
            return base64.b64decode(text)
        except Exception:
            return None

    def file_state(self, path: str) -> str:
        from core.ssh import shquote
        quoted = shquote(path)
        cmd = (f"if [ -e {quoted} ]; then "
               f"if [ -s {quoted} ]; then echo nonempty; else echo empty; fi; "
               f"else echo missing; fi")
        try:
            out = (self._ctx.remote.run(cmd, timeout=20, record=False).stdout or "").strip()
        except Exception:
            return "error"
        if out == "missing":
            return "missing"
        if out == "empty":
            return "empty"
        if out == "nonempty":
            return "ok"
        # 命令本身没跑成（SSH 不可达/超时）：传输层故障，不是「没启用」
        return "error"

    # ---- 环境 ----

    def environment_prefix(self) -> str:
        from core.remote import ros_env_prefix
        return ros_env_prefix(self._ctx.profile)


class UnsupportedPlatform(BasePlatform):
    """平台不支持时的占位。

    每个方法返回 `None` / ``error``，让检测项自然落到 SKIP。
    **不伪造数据，也不抛异常**：缺平台能力应表现为「未验证」，
    而不是把整轮检测拖垮。
    """

    def __init__(self, reason: str = ""):
        self.reason = reason

    def measure_topic_hz(self, topic: str, seconds: int = 5) -> Optional[float]:
        return None

    def echo_once(self, topic: str, field: str = "",
                  timeout: int = 12) -> Optional[str]:
        return None

    def run_probe(self, name: str, *args: str, timeout: int = 90,
                  needs_ros: bool = True) -> Optional[Dict[str, Any]]:
        return None

    def read_bytes(self, path: str, max_bytes: Optional[int] = None) -> Optional[bytes]:
        return None

    def file_state(self, path: str) -> str:
        return "error"

    def environment_prefix(self) -> str:
        return ""


# 已注册的平台。新增平台在这里登记，未知名字明确报错而不是回退默认 ——
# 静默回退会让「以为在测 A 平台、实际跑在 B 平台」这种错误无从发现。
_PLATFORMS = {
    "ros1": Ros1Platform,
}


def platform_for(ctx: Any, kind: str = "ros1") -> BasePlatform:
    """按类型取平台实现。

    未知类型抛 `ValueError`。不返回 `UnsupportedPlatform`：
    配置写错要立刻暴露，而不是让整轮检测静默 SKIP。
    """
    cls = _PLATFORMS.get(kind)
    if cls is None:
        known = "、".join(sorted(_PLATFORMS))
        raise ValueError(f"未知平台类型 {kind!r}，已注册：{known}")
    return cls(ctx)


def registry() -> List[str]:
    """已注册的平台名，供自检与文档核对。"""
    return sorted(_PLATFORMS)
