"""协议层解析测试。

核心目的不是「证明代码能跑」，而是「证明帧长公式与 C 侧 buildCmd 一致」。
其他地方写错还能改，帧长差一个字节就是全线解析失败。

⚠️ 自洽测试（build_frame → FrameParser）只能证明本模块内部一致，
   不能证明与真实下位机一致。真实一致性要靠实车抓包，见
   test_frame_length_matches_buildcmd 的注释。
"""
import struct

import pytest

from core.protocol import (
    HEAD,
    HEADER_LEN,
    LEN_TO_TOTAL,
    FrameParser,
    ParseStats,
    build_frame,
    compute_bcc,
    iter_frames,
    pack_type_name,
)

VOLTAGE = 0xC001
HEART_BEAT = 0x0000
IMU = 0x8003
ODOM = 0x8000


# ---------------------------------------------------------------------------
# 帧长公式 —— 与 C 侧 buildCmd 对照
# ---------------------------------------------------------------------------

def test_frame_length_matches_buildcmd():
    """逐字节展开 protocol.cpp:buildCmd，与 build_frame 对比。

    C 侧对 payload=b"\\x00\\x00\\x80\\x3f"（float 1.0）、cmd=0xC001：

        buf[0] = 0xAA55 & 0xFF        = 0x55
        buf[1] = (0xAA55 >> 8) & 0xFF = 0xAA
        len    = 2 + 4                = 6
        buf[2] = 6 & 0xFF             = 0x06
        buf[3] = 6 >> 8               = 0x00
        buf[4] = 0xC001 & 0xFF        = 0x01
        buf[5] = 0xC001 >> 8          = 0xC0
        buf[6..9]  = payload
        buf[10]    = bcc = XOR(buf[4..9])

    总长 11 字节，而 len 字段是 6 → 总长 = len + 5。这就是 LEN_TO_TOTAL。

    如果将来实车抓包发现 bcc 大面积校验失败，优先回来改 LEN_TO_TOTAL。
    """
    payload = struct.pack("<f", 1.0)
    expected = bytes([0x55, 0xAA, 0x06, 0x00, 0x01, 0xC0]) + payload
    bcc = 0x01 ^ 0xC0
    for byte in payload:
        bcc ^= byte
    expected += bytes([bcc & 0xFF])

    assert build_frame(VOLTAGE, payload) == expected
    assert len(expected) == 11
    assert LEN_TO_TOTAL + 6 == 11


def test_bcc_excludes_head_and_len():
    """bcc 只覆盖 pack_type 与 payload —— C 侧从 buf[4] 开始 XOR。"""
    payload = b"\x01\x02\x03"
    # 手工算：0x0001 低字节 ^ 高字节 ^ 01 ^ 02 ^ 03
    assert compute_bcc(0x0001, payload) == (0x01 ^ 0x00 ^ 0x01 ^ 0x02 ^ 0x03)
    # 换 pack_type 高字节应该改变 bcc
    assert compute_bcc(0x0101, payload) != compute_bcc(0x0001, payload)


# ---------------------------------------------------------------------------
# 基本解析
# ---------------------------------------------------------------------------

def test_single_frame_roundtrip():
    payload = struct.pack("<f", 12.34)
    data = build_frame(VOLTAGE, payload)
    frames = list(iter_frames(data))

    assert len(frames) == 1
    f = frames[0]
    assert f.pack_type == VOLTAGE
    assert f.name == "Voltage_RESPONSE"
    assert f.payload == payload
    assert f.bcc_ok is True
    assert f.raw == data
    assert f.voltage() == pytest.approx(12.34, abs=1e-4)


def test_multiple_frames_in_one_chunk():
    parts = [build_frame(HEART_BEAT), build_frame(VOLTAGE, struct.pack("<f", 11.5))]
    frames = list(iter_frames(b"".join(parts)))
    assert [f.name for f in frames] == ["HEART_BEAT", "Voltage_RESPONSE"]
    assert frames[1].voltage() == pytest.approx(11.5)


def test_half_frame_waits_for_rest():
    """半帧不能被丢掉，也不能提前吐出来。"""
    data = build_frame(VOLTAGE, struct.pack("<f", 12.0))
    parser = FrameParser()

    first = parser.feed(data[:5])
    assert first == []
    assert parser.pending == 5

    rest = parser.feed(data[5:])
    assert len(rest) == 1
    assert rest[0].bcc_ok is True
    assert parser.pending == 0


def test_byte_by_byte_feed():
    """逐字节喂也要能拼出来 —— 串口读到的不保证是整帧。"""
    data = build_frame(VOLTAGE, struct.pack("<f", 9.9))
    parser = FrameParser()
    got = []
    for i in range(len(data)):
        got.extend(parser.feed(data[i:i + 1]))
    assert len(got) == 1
    assert got[0].voltage() == pytest.approx(9.9, abs=1e-4)


# ---------------------------------------------------------------------------
# 错误场景 —— 这些才是真实串口会遇到的情况
# ---------------------------------------------------------------------------

def test_bcc_error_is_reported_not_dropped():
    """bcc 错的帧要吐出来并标记，不能静默丢弃。

    静默丢弃会让「线路噪声」这个诊断信息消失，
    最后只剩一句「没收到数据」。
    """
    data = bytearray(build_frame(VOLTAGE, struct.pack("<f", 12.0)))
    data[-1] ^= 0xFF          # 破坏 bcc
    frames = list(iter_frames(bytes(data)))

    assert len(frames) == 1
    assert frames[0].bcc_ok is False
    assert frames[0].name == "Voltage_RESPONSE"


def test_garbage_prefix_is_dropped():
    stats = ParseStats()
    data = b"\xde\xad\xbe\xef" + build_frame(VOLTAGE, struct.pack("<f", 12.0))
    frames = list(iter_frames(data, stats))

    assert len(frames) == 1
    assert frames[0].bcc_ok is True
    assert stats.dropped_bytes == 4


def test_oversized_length_is_rejected():
    """畸形 len 不能让解析器去分配巨量内存。"""
    stats = ParseStats()
    # len = 0xFFFF，合法头部但长度离谱
    bad = HEAD + b"\xff\xff" + b"\x01\xc0" + b"\x00" * 8
    frames = list(iter_frames(bad, stats))

    assert frames == []
    assert stats.length_errors >= 1


def test_tiny_length_is_rejected():
    """len < 2 的帧在物理上不可能，属于伪包头。"""
    stats = ParseStats()
    bad = HEAD + b"\x00\x00" + b"\x00\x00" + b"\x00" * 4
    frames = list(iter_frames(bad, stats))
    assert frames == []
    assert stats.length_errors >= 1


def test_fake_head_inside_payload_resyncs():
    """payload 里恰好出现 55 AA 时，若前面的帧头被破坏，需要能重新对齐。

    这里构造：一个坏帧头 + 一个完整好帧。解析器丢字节后应能找到好帧。
    """
    good = build_frame(VOLTAGE, struct.pack("<f", 8.8))
    # 前面塞一个看起来像包头但长度非法的片段
    noise = bytes([0x55, 0xAA, 0xFF, 0xFF])
    frames = list(iter_frames(noise + good))

    ok_frames = [f for f in frames if f.bcc_ok]
    assert len(ok_frames) == 1
    assert ok_frames[0].voltage() == pytest.approx(8.8, abs=1e-4)


# ---------------------------------------------------------------------------
# 解码
# ---------------------------------------------------------------------------

def test_voltage_decode():
    frames = list(iter_frames(build_frame(VOLTAGE, struct.pack("<f", 12.1))))
    assert frames[0].decode()["values"][0] == pytest.approx(12.1, abs=1e-4)


def test_voltage_wrong_payload_length_returns_none():
    """payload 长度不对时 voltage() 返回 None，不能瞎解。"""
    frames = list(iter_frames(build_frame(VOLTAGE, b"\x00\x00")))
    assert frames[0].voltage() is None
    assert frames[0].decode() is None


def test_odom_decode():
    payload = struct.pack("<10f", *[float(i) for i in range(10)])
    frames = list(iter_frames(build_frame(ODOM, payload)))
    values = frames[0].decode()["values"]
    assert values[0] == 0.0
    assert values[9] == 9.0


def test_imu_decode():
    payload = struct.pack("<10f", *[0.5] * 10)
    frames = list(iter_frames(build_frame(IMU, payload)))
    assert len(frames[0].decode()["values"]) == 10


def test_heartbeat_has_no_decoder():
    """心跳没有独立结构体，decode 返回 None 但不报错。"""
    frames = list(iter_frames(build_frame(HEART_BEAT)))
    assert frames[0].name == "HEART_BEAT"
    assert frames[0].decode() is None


# ---------------------------------------------------------------------------
# 统计与诊断
# ---------------------------------------------------------------------------

def test_stats_counts_by_type():
    stats = ParseStats()
    data = (
        build_frame(HEART_BEAT)
        + build_frame(HEART_BEAT)
        + build_frame(VOLTAGE, struct.pack("<f", 12.0))
    )
    list(iter_frames(data, stats))
    d = stats.as_dict()

    assert d["frames"] == 3
    assert d["bcc_ok"] == 3
    assert d["by_type"]["HEART_BEAT"] == 2
    assert d["by_type"]["Voltage_RESPONSE"] == 1


def test_unknown_type_is_counted_not_crashed():
    """固件比软件新时会出现未知包类型，不能崩。"""
    stats = ParseStats()
    frames = list(iter_frames(build_frame(0x7777, b"\x01\x02"), stats))

    assert len(frames) == 1
    assert frames[0].name == "UNKNOWN_0x7777"
    assert stats.unknown_types == 1


def test_pack_type_name_known():
    assert pack_type_name(0xC001) == "Voltage_RESPONSE"
    assert pack_type_name(0x8003) == "IMU_REPONSE"
    assert pack_type_name(0x1234) == "UNKNOWN_0x1234"


def test_stats_survives_truncated_stream():
    """日志被截断（比如进程被 kill）时不能抛异常。"""
    data = build_frame(VOLTAGE, struct.pack("<f", 12.0)) + b"\x55\xAA\x06"
    stats = ParseStats()
    frames = list(iter_frames(data, stats))
    # 完整帧解析出来，截断的半帧留在缓冲里不报错
    assert len(frames) == 1
    assert stats.frames == 1


def test_buffer_does_not_grow_without_bound():
    """串口半途断线时，一块「长度合法但永远凑不齐」的数据不能无限涨。

    只看「缓冲有界」——不要断言「清空后立刻能解析新帧」：残留的伪帧头
    会先声明一个长长度把后续字节吞进去，要凑够才会被判坏帧。
    """
    from core.protocol import BUF_MAX

    stats = ParseStats()
    parser = FrameParser(stats)
    # 反复喂一个声明 len=4095、但永远只有个头的帧
    head = bytes((0x55, 0xAA, 0xFF, 0x0F, 0x00, 0x80))
    for _ in range(BUF_MAX):
        parser.feed(head)
        assert len(parser._buf) <= BUF_MAX, "缓冲没有被看仓清掉"
    assert stats.dropped_bytes > 0
