"""第 20 项「通讯链路（协议层）」的判定测试。

解析器本身由 `test_protocol.py` 覆盖（帧长、bcc、半帧、粘包）。
这里只测**判定逻辑**：拿到什么日志，就得出什么结论。

重点防一处已发生的错误结论：
源码里没有电压查询调用，就被推断成「下位机永不发电压」。
2026-11-06 实车证明下位机会主动低频上报（20 秒 3 帧）。
所以「上行没查询」绝不等于「下行不会有回传」，短窗口没捕到也不能判死。
"""

import base64
import struct

import pytest

from checks import protocol as check
from core.protocol import build_frame
from core.verdict import Status

VOLTAGE = 0xC001
IMU = 0x8003
ODOM = 0x8000


class _Resp:
    def __init__(self, stdout: str = ""):
        self.stdout = stdout
        self.stderr = ""
        self.ok = True


class _TapRemote:
    """把 tap 日志文件当作 base64 回给 _fetch 的假远端。"""

    host = "127.0.0.1"
    user = "test"

    def __init__(self, files):
        self.files = files
        self.commands = []

    def run(self, cmd, timeout=30, record=True):
        self.commands.append(cmd)
        for name, data in self.files.items():
            if name in cmd:
                return _Resp(base64.b64encode(data).decode())
        return _Resp("")

    def close(self):
        pass


class _Ctx:
    tap_dir = "/tmp/tap"

    def __init__(self, files):
        self.remote = _TapRemote(files)


def _odom(value=0.0):
    return build_frame(ODOM, struct.pack("<10f", *([value] * 10)))


def _imu(value=0.0):
    return build_frame(IMU, struct.pack("<10f", *([value] * 10)))


def _voltage(value: float):
    return build_frame(VOLTAGE, struct.pack("<f", value))


def _run(files):
    return check.run(_Ctx(files))


def _names(result):
    return [m.name for m in result.measurements]


def _measure(result, name):
    return next(m for m in result.measurements if m.name == name)


# ---------------------------------------------------------------------------
# 采集未启用
# ---------------------------------------------------------------------------

def test_no_tap_log_gives_skip():
    result = _run({})
    assert result.status is Status.SKIP
    assert result.number == 20


# ---------------------------------------------------------------------------
# 链路正常
# ---------------------------------------------------------------------------

def test_healthy_link_passes_and_reports_voltage():
    down = _odom() + _imu() + _voltage(11.71) + _voltage(11.68)
    result = _run({"from_mcu.raw": down})

    assert result.status is Status.PASS
    assert "bcc 错误 0" in result.summary
    volts = _measure(result, "电压回传")
    assert volts.value == "11.68~11.71"
    assert volts.unit == "V"
    # 报告帧数与类型数，便于判断「有没有在发」
    assert "下行帧数" in _names(result)


def test_link_without_voltage_frames_still_passes():
    """没捕到电压帧不影响链路判定 —— 链路是活的。"""
    result = _run({"from_mcu.raw": _odom() + _imu()})
    assert result.status is Status.PASS


# ---------------------------------------------------------------------------
# 电压诊断：不能把「没捕到」说成「从不会发」
# ---------------------------------------------------------------------------

def test_missing_voltage_without_query_does_not_claim_request_response():
    """上行没有电压查询时，不能断言「不发请求就不会有回传」。

    实车证明下位机主动低频上报，这条断言会在短窗口下变成假结论。
    """
    result = _run({"from_mcu.raw": _odom() + _imu(), "to_mcu.raw": b""})

    note = _measure(result, "电压诊断").note
    assert "不等于不回传" in note
    assert "不发请求就不会有回传" not in note


def test_missing_voltage_with_query_reports_no_response():
    """上位机确实发过电压查询，才能判「下位机没响应」。"""
    up = _voltage(0.0)  # 同样用 0xC001 作为查询类型
    result = _run({"from_mcu.raw": _odom(), "to_mcu.raw": up})

    diag = _measure(result, "电压诊断")
    assert diag.value == "已发查询但本窗口内无回传"


# ---------------------------------------------------------------------------
# 故障判定
# ---------------------------------------------------------------------------

def test_bad_bcc_ratio_fails():
    good = _odom()
    bad = bytearray(good)
    bad[-1] ^= 0xFF  # 破坏 bcc
    down = good + bytes(bad) * 3

    result = _run({"from_mcu.raw": down})
    assert result.status is Status.FAIL
    assert "bcc" in result.summary


def test_undecodable_downlink_fails():
    """有字节但一帧都解析不出 —— 是格式/波特率问题，不是「没数据」。"""
    result = _run({"from_mcu.raw": b"\x00" * 256})
    assert result.status is Status.FAIL
    assert "无任何可解析回传帧" in result.summary


def test_mixed_errors_under_limit_still_pass():
    """偶发噪声（1 帧错 / 大量正常）在 1% 容忍内应判 PASS。"""
    down = _odom() * 200
    bad = bytearray(_odom())
    bad[-1] ^= 0xFF
    result = _run({"from_mcu.raw": down + bytes(bad)})

    assert result.status is Status.PASS
    assert _measure(result, "bcc 错误").value == "1"
