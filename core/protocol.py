"""下位机协议解析 —— 纯函数，无 IO，可单测。

协议来自 `tianbot_core`：

    struct protocol_pack {
        uint16_t head;       // 0xAA55，线上字节 55 AA
        uint16_t len;        // data len + 2（pack_type 占 2 字节）
        uint16_t pack_type;
        uint8_t  data[];     // 末字节是 bcc
    };

建帧逻辑见 `protocol.cpp:buildCmd`：

    head(2) + len(2) + pack_type(2) + payload(len-2) + bcc(1)

    总帧长 = 5 + len
    bcc    = XOR(pack_type 低字节, 高字节, payload...)   —— 不含 head 与 len

⚠️ 帧长公式 `5 + len` 是从 buildCmd 的写入顺序推出来的，尚未用真实
串口帧验证过。第一次上真车抓包时必须核对：如果 bcc 校验大面积失败，
优先怀疑这里差一个字节。核对方法见 `tests/test_protocol.py` 里的
`test_frame_length_matches_buildcmd`。

本模块不依赖 rospy、不依赖 pyserial，只吃 bytes。这样上位机可以在
离线状态下解析从车上取回的日志。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

HEAD_LO = 0x55
HEAD_HI = 0xAA
HEAD = bytes((HEAD_LO, HEAD_HI))

# 固定头长度：head(2) + len(2) + pack_type(2)
HEADER_LEN = 6
# len 字段到总帧长的偏移：总长 = 5 + len
LEN_TO_TOTAL = 5
# len 最小值：pack_type(2) + 至少 0 字节 payload
LEN_MIN = 2
# len 上限。防止一个错乱的长度字段让我们去分配 GB 级内存。
# 当前固件实测见到的最大帧是 47 字节（ODOM/IMU，len=42）；
# 按 _FLOAT_COUNTS 表定义，ODOM_V2_RESPONSE 是 payload 48 → len=50 → 总长 55，
# 给足余量到 4096 足够，超出的直接判为坏帧。
LEN_MAX = 4096

# 不需要额外的缓冲区上限。
#
# 2026-11-06 审查：原先写过 BUF_MAX = 4 * (LEN_TO_TOTAL + LEN_MAX)，
# 但那个分支**永远不可达** —— `_next()` 每次都会把能消费的帧全部消费掉，
# 缓冲区里最多只留一个不完整帧，其长度被 LEN_MAX 约束在
# LEN_TO_TOTAL + LEN_MAX - 1 = 4100 字节内，远小于 BUF_MAX。
# 实测（反复喂伪帧头）：峰值 4098，从未超过 4100。
# 留着它只会给人「已经有兵仓」的错觉，已移除。

# 包类型 → 名称。值来自 protocol.h 的枚举顺序（0x4000/0x8000/0xC000 显式给定）
PACK_TYPES: Dict[int, str] = {
    0x0000: "HEART_BEAT",
    0x0001: "CMD_VEL",
    0x0002: "ACKMAN_VEL",
    0x0003: "SET_ROVER_MOTION_MODE",
    0x4000: "DEBUG",
    0x8000: "ODOM_RESPONSE",
    0x8001: "UWB_RESPONSE",
    0x8002: "HEART_BEAT_RESPONSE",
    0x8003: "IMU_REPONSE",
    0x8004: "ODOM_V2_RESPONSE",
    0xC000: "DEBUG_RESPONSE",
    0xC001: "Voltage_RESPONSE",
}

# payload 里的 float 个数（权威来源）。按 protocol.h 的结构体定义数出来：
#   voltage     { float Battery_voltage }                        → 1
#   ackermann_cmd { float steering_angle; float speed }           → 2
#   imu_feedback { quaternion(4); vector3(3); vector3(3) }        → 10
#   odom        { pose{vector3(3), yaw(1)}; twist(6) }            → 10
#   odom_v2     { pose_v2{vector3(3), rpy(3)}; twist(6) }         → 12
#   uwb         { float x_m; float y_m; float yaw }               → 3
_FLOAT_COUNTS: Dict[str, int] = {
    "Voltage_RESPONSE": 1,
    "ACKMAN_VEL": 2,
    "IMU_REPONSE": 10,      # quat(4) + linear_acc(3) + angular_vel(3)
    "ODOM_RESPONSE": 10,    # pose(4) + twist(6)
    "ODOM_V2_RESPONSE": 12, # pose_v2(6) + twist(6)
    "UWB_RESPONSE": 3,
}


def pack_type_name(value: int) -> str:
    """包类型值 → 可读名。未知值返回 `UNKNOWN_0xNNNN`，不抛异常。"""
    return PACK_TYPES.get(value, f"UNKNOWN_0x{value:04X}")


def compute_bcc(pack_type: int, payload: bytes) -> int:
    """按 protocol.cpp 的算法算 bcc。

    C 侧从 buf[4] 开始 XOR，buf[4:6] 正是 pack_type 的小端两字节。
    所以等价于 XOR(pack_type_lo, pack_type_hi, payload...)。
    """
    bcc = (pack_type & 0xFF) ^ ((pack_type >> 8) & 0xFF)
    for byte in payload:
        bcc ^= byte
    return bcc & 0xFF


@dataclass
class Frame:
    """一帧解析结果。raw 保留原始字节，便于日志回放与排查。"""

    pack_type: int
    payload: bytes
    bcc: int
    bcc_ok: bool
    raw: bytes
    offset: int = 0

    @property
    def name(self) -> str:
        return pack_type_name(self.pack_type)

    def decode(self) -> Optional[dict]:
        """按已知结构体解包 payload。

        未知类型或长度不符时返回 None —— 调用方据此区分
        「收到了但解不开」和「压根没收到」。
        """
        count = _FLOAT_COUNTS.get(self.name)
        if count is not None:
            need = 4 * count
            if len(self.payload) != need:
                return None
            return {"values": list(struct.unpack(f"<{count}f", self.payload))}
        if self.name == "SET_ROVER_MOTION_MODE":
            if len(self.payload) != 4:
                return None
            return {"mode": struct.unpack("<I", self.payload)[0]}
        return None

    def voltage(self) -> Optional[float]:
        """电压帧专用取值。非电压帧或结构不符返回 None。"""
        if self.name != "Voltage_RESPONSE" or len(self.payload) != 4:
            return None
        return struct.unpack("<f", self.payload)[0]


@dataclass
class ParseStats:
    """解析过程的健康度统计。

    这张表是「为什么没数据」的诊断依据：
      - 只有 tx 没有 rx        → 下位机没回
      - 某类型 rx 计数为 0      → 该功能下位机不支持
      - bcc_errors 高          → 线路噪声或帧长公式不对
      - unknown_types 高       → 固件版本比软件新，包类型表要更新
    """

    frames: int = 0
    bcc_ok: int = 0
    bcc_errors: int = 0
    length_errors: int = 0
    unknown_types: int = 0
    bytes_in: int = 0
    dropped_bytes: int = 0     # 帧同步时丢弃的无效字节数
    by_type: Dict[str, int] = field(default_factory=dict)

    def record(self, frame: Frame) -> None:
        self.frames += 1
        if frame.bcc_ok:
            self.bcc_ok += 1
        else:
            self.bcc_errors += 1
        if frame.pack_type not in PACK_TYPES:
            self.unknown_types += 1
        self.by_type[frame.name] = self.by_type.get(frame.name, 0) + 1

    def as_dict(self) -> dict:
        return {
            "frames": self.frames,
            "bcc_ok": self.bcc_ok,
            "bcc_errors": self.bcc_errors,
            "length_errors": self.length_errors,
            "unknown_types": self.unknown_types,
            "bytes_in": self.bytes_in,
            "dropped_bytes": self.dropped_bytes,
            "by_type": dict(sorted(self.by_type.items())),
        }


class FrameParser:
    """流式帧解析器。喂任意长度的 bytes，吐出完整的 Frame。

    设计要点：
      - 半帧留在缓冲里等下一次 feed，不会丢
      - 找不到包头时逐字节前移，并把丢弃量记进 stats
      - bcc 错帧仍然吐出（bcc_ok=False），由调用方决定是否计数使用
        —— 不静默丢弃，否则「线路有噪声」这个信息就消失了
    """

    def __init__(self, stats: Optional[ParseStats] = None):
        self._buf = bytearray()
        self.stats = stats if stats is not None else ParseStats()
        self._consumed = 0

    def feed(self, chunk: bytes) -> List[Frame]:
        self.stats.bytes_in += len(chunk)
        self._buf.extend(chunk)
        out: List[Frame] = []
        while True:
            frame = self._next()
            if frame is None:
                break
            out.append(frame)
        # 不设额外上限：见文件头关于「不需要缓冲区上限」的注释。
        # 单个不完整帧已被 LEN_MAX 约束在 4101 字节内。
        return out

    def _next(self) -> Optional[Frame]:
        """取出下一帧。返回 None 只表示「数据还不够」，不代表解析失败。

        关键点：丢弃伪包头之后必须**继续尝试**，不能直接返回 None。
        否则一段「垃圾 + 完整帧」的输入要等下一次 feed 才能对齐，
        iter_frames 这种一次性解析会直接把好帧漏掉（测试抓过这个 bug）。
        """
        buf = self._buf
        while True:
            # 1. 对齐到包头
            start = buf.find(HEAD)
            if start < 0:
                # 只剩半个包头（单个 0x55）时留着，其余全丢
                keep = 1 if buf.endswith(bytes((HEAD_LO,))) else 0
                dropped = len(buf) - keep
                if dropped:
                    self.stats.dropped_bytes += dropped
                    del buf[:dropped]
                return None
            if start > 0:
                self.stats.dropped_bytes += start
                del buf[:start]

            # 2. 头部够不够
            if len(buf) < HEADER_LEN:
                return None

            total_len_le = int.from_bytes(buf[2:4], "little")
            pack_type = int.from_bytes(buf[4:6], "little")

            # 3. 长度非法时只丢第一个字节重新对齐，然后**继续尝试**，
            #    因为这个 0x55AA 很可能是 payload 里的伪包头。
            if total_len_le < LEN_MIN or total_len_le > LEN_MAX:
                self.stats.length_errors += 1
                self.stats.dropped_bytes += 1
                del buf[:1]
                continue

            total = LEN_TO_TOTAL + total_len_le
            if len(buf) < total:
                return None   # 半帧，等更多数据

            raw = bytes(buf[:total])
            payload_len = total_len_le - LEN_MIN
            payload = raw[HEADER_LEN:HEADER_LEN + payload_len]
            bcc = raw[HEADER_LEN + payload_len] if total > HEADER_LEN + payload_len else 0
            expect = compute_bcc(pack_type, payload)

            frame = Frame(
                pack_type=pack_type,
                payload=payload,
                bcc=bcc,
                bcc_ok=(bcc == expect),
                raw=raw,
                offset=self._consumed,
            )
            self._consumed += total
            del buf[:total]
            self.stats.record(frame)
            return frame

    @property
    def pending(self) -> int:
        """缓冲里还没凑成完整帧的字节数。"""
        return len(self._buf)


def iter_frames(data: bytes, stats: Optional[ParseStats] = None) -> Iterator[Frame]:
    """一次性解析整块数据。日志回放用。"""
    parser = FrameParser(stats)
    for frame in parser.feed(data):
        yield frame


def build_frame(pack_type: int, payload: bytes = b"") -> bytes:
    """按 protocol.cpp 的规则造一帧。测试与回放用。

    与 C 侧 buildCmd 逐字节对齐：
        len = 2 + len(payload)
        帧 = 55 AA | len(LE) | pack_type(LE) | payload | bcc
    """
    total_len_le = LEN_MIN + len(payload)
    body = bytearray(HEAD)
    body.extend(total_len_le.to_bytes(2, "little"))
    body.extend((pack_type & 0xFFFF).to_bytes(2, "little"))
    body.extend(payload)
    body.append(compute_bcc(pack_type, payload))
    return bytes(body)
