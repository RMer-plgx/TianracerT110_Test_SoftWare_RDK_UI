"""测试共用的产品配置档替身。

为什么需要
----------
2026-11-03 多产品重构后，所有 checks 都通过 `ctx.profile` 取话题、
工作空间等产品相关信息。测试里的 `FakeCtx` 若不带 profile，
会大面积 `AttributeError: 'FakeCtx' object has no attribute 'profile'`
—— 40 个测试同时红，但根因只有一个。

这里提供两种用法：

1. `make_ctx(...)` —— 直接拿到一个带 profile 的 Context 替身
2. `attach_profile(obj)` —— 给已有的 FakeCtx 补上 profile 属性

两者都返回**真实的 Profile 对象**（加载自 products/），
不造假数据。这样测试同时验证了「profile 能被正确加载」。
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import profile as product_profile  # noqa: E402

# 加载一次，所有测试共用（profile 是只读的）
_PROFILE = None


def get_profile():
    """默认产品的 Profile 实例。"""
    global _PROFILE
    if _PROFILE is None:
        _PROFILE = product_profile.load("tianracer_t110_rdk")
    return _PROFILE


def attach_profile(ctx):
    """给测试用的 ctx 替身补上 profile 属性。

    若已有 profile 就不动。返回同一个对象，便于链式使用。
    """
    if getattr(ctx, "profile", None) is None:
        try:
            ctx.profile = get_profile()
        except AttributeError:
            # object() 之类的不可变对象，调用方应改用 `profile = get_profile()`
            raise TypeError(
                "attach_profile 需要一个可变对象。"
                "类属性请直接写 `profile = get_profile()`。"
            ) from None
    return ctx


def topic(key: str) -> str:
    """取默认产品的某个话题名，供断言使用。"""
    return get_profile().ros.topic(key)


def baseline() -> dict:
    """默认产品的 baseline 字典。"""
    return get_profile().baseline
