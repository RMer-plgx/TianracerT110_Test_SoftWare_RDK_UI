"""`tools/tap_probe.py` 的纯函数部分。

这个工具要连车，整体没法在单测里跑；但它的**停滞检测**是纯函数，
而且有个很容易写错的点：输出文件可能恰好读到半行。

Round 10 审查指出：`json.loads` 失败就 `return None`，上层只能
`continue`，于是 `stalled` 既不增也不清 —— 停滞检测会永久失效。
这里把修复钉住。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "tools" / "tap_probe.py"


@pytest.fixture(scope="module")
def probe():
    spec = importlib.util.spec_from_file_location("tap_probe_under_test", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reads_last_stats_line(probe):
    out = (
        "TAP_READY /dev/pts/1\n"
        'TAP_STATS {"to_mcu": 5, "from_mcu": 9}\n'
        'TAP_STATS {"to_mcu": 50, "from_mcu": 90}\n'
    )
    assert probe._last_stats_progress(out) == (50, 90)


def test_partial_trailing_line_falls_back_to_previous(probe):
    """尾部半行时不能返回 None —— 要往前找上一个可解析的 STATS。"""
    out = (
        'TAP_STATS {"to_mcu": 1, "from_mcu": 2}\n'
        'TAP_STATS {"to_mcu": 3, "from'
    )
    assert probe._last_stats_progress(out) == (1, 2)


def test_garbage_line_is_skipped(probe):
    out = (
        'TAP_STATS {"to_mcu": 1, "from_mcu": 2}\n'
        "通信垃圾\n"
        "TAP_ERROR something\n"
    )
    assert probe._last_stats_progress(out) == (1, 2)


def test_no_stats_yet_returns_none(probe):
    """还没出现任何 STATS：返回 None，让上层跳过这一轮比较。"""
    assert probe._last_stats_progress("TAP_READY /dev/pts/1\n") is None
    assert probe._last_stats_progress("") is None


def test_non_dict_payload_is_skipped(probe):
    """JSON 能解析但不是对象，同样要跳过。"""
    out = (
        "TAP_STATS [1, 2]\n"
        'TAP_STATS {"to_mcu": 7, "from_mcu": 8}\n'
    )
    assert probe._last_stats_progress(out) == (7, 8)


def test_missing_keys_default_to_zero(probe):
    assert probe._last_stats_progress('TAP_STATS {"to_mcu": 3}\n') == (3, 0)
