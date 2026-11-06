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
    """把 tap 日志文件当作 base64 回给 _fetch 的假远端。

    `_fetch` 先发 `test -s <path>` 判断文件在不在，再取内容，
    所以夹具要同时模拟这两步，否则会被当成「传输失败」。
    """

    host = "127.0.0.1"
    user = "test"

    def __init__(self, files, unreachable=False, present_empty=()):
        self.files = files
        self.unreachable = unreachable      # 模拟 SSH 不可达
        self.present_empty = set(present_empty)  # 模拟存在但为空
        self.commands = []

    def _target(self, cmd):
        for name in self.files:
            if name in cmd:
                return name
        for name in self.present_empty:
            if name in cmd:
                return name
        return None

    def run(self, cmd, timeout=30, record=True):
        self.commands.append(cmd)
        if self.unreachable:
            return _Resp("")
        name = self._target(cmd)
        if name is None:
            return _Resp("no")
        if cmd.startswith("test -s"):
            # -s 是「存在且非空」；空文件应该走 -e 分支变成 empty
            return _Resp("yes" if self.files.get(name) else "no")
        if cmd.startswith("test -e"):
            return _Resp("yes")
        data = self.files.get(name, b"")
        return _Resp(base64.b64encode(data).decode())

    def close(self):
        pass


class _Ctx:
    tap_dir = "/tmp/tap"

    def __init__(self, files, **kwargs):
        self.remote = _TapRemote(files, **kwargs)


def _odom(value=0.0):
    return build_frame(ODOM, struct.pack("<10f", *([value] * 10)))


def _imu(value=0.0):
    return build_frame(IMU, struct.pack("<10f", *([value] * 10)))


def _voltage(value: float):
    return build_frame(VOLTAGE, struct.pack("<f", value))


def _run(files, **kwargs):
    return check.run(_Ctx(files, **kwargs))


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


# ---------------------------------------------------------------------------
# 传输层故障不能被当成「未启用采集」
# ---------------------------------------------------------------------------

def test_unreachable_ssh_is_fail_not_skip():
    """SSH 连不上时不能判 SKIP —— 那会把一次故障藏起来。"""
    result = _run({"from_mcu.raw": _odom()}, unreachable=True)

    assert result.status is Status.FAIL
    assert "取回失败" in result.summary


def test_present_but_empty_logs_are_skip():
    """文件在、但两个方向都是 0 字节 —— 真正的「没采到数据」。"""
    result = _run({}, present_empty=("from_mcu.raw", "to_mcu.raw"))

    assert result.status is Status.SKIP


# ---------------------------------------------------------------------------
# 上行空白要报出来
# ---------------------------------------------------------------------------

def test_downlink_only_flags_missing_uplink():
    """下行有帧、上行完全空白：链路活着，但底盘节点可能没接到 pty。"""
    result = _run({"from_mcu.raw": _odom() + _imu()})

    assert result.status is Status.PASS
    assert "上行告警" in _names(result)
    assert "上行无可解析帧" in result.summary


def test_both_directions_healthy_has_no_uplink_warning():
    result = _run({"from_mcu.raw": _odom() + _imu(),
                   "to_mcu.raw": _odom()})

    assert result.status is Status.PASS
    assert "上行告警" not in _names(result)
    assert "上行无可解析帧" not in result.summary


# ---------------------------------------------------------------------------
# 报告可读性
# ---------------------------------------------------------------------------

def test_type_breakdown_is_compact_not_raw_dict():
    """类型分布要拼成短文本，不能把整个 dict 塑进 note。"""
    result = _run({"from_mcu.raw": _odom() + _imu() + _voltage(11.7)})
    note = _measure(result, "下行帧数").note

    assert "{" not in note and "'" not in note
    assert "ODOM_RESPONSE×" in note
    assert len(note) <= 80
