#!/usr/bin/env python3
"""串口桥接嗅探器 —— 让检测软件能看到下位机的原始协议帧。

问题背景
--------
检测软件原来只能读 ROS 话题。话题没数据时无法区分「下位机没发」、
「ROS 层丢了」还是「解析错了」。电压项就是典型：话题 advertise 了但
永不发布，四种成因在报告里显示成同一句话。

实现方式
--------
在真实串口和底盘节点之间插一层 pty（伪终端）：

    底盘 MCU ──串口──> /dev/ttyUSB0
                          │
                    [本脚本双向转发]
                          │
                    /dev/pts/N ──> 底盘节点（env TIANRACER_BASE_PORT）

底盘节点本来就从 `TIANRACER_BASE_PORT` 取串口路径，所以只要把环境变量
指到本脚本报出来的 /dev/pts/N 即可，**不需要改任何车上源码**。

可行性依据（已在实车源码确认）
------------------------------
`serial.cpp:209-218` 会主动设 `c_oflag &= ~OPOST`、
`c_lflag &= ~(ICANON|ECHOE|ECHO|ISIG)`、`VMIN=1 VTIME=1`。
即底盘节点自己会把终端切成接近 raw 的模式，pty 能正确承载二进制帧。

安全约定（最重要）
------------------
桥接进程是控制链路上的单点。因此：

1. **绝不修改字节**。原样转发，连 bcc 错帧也照转 —— 我们不解释协议，
   只搬运。任何「修复」都可能把好帧改坏。
2. **退出即安全**。本脚本退出/崩溃后，底盘节点写 pty 会失败并进入
   ROS 的报错路径，不会凭空产生运动指令。上位机侧必须监听 `TAP_EXIT`
   并立刻停 bringup。
3. **写日志有上限**。默认每方向 32 MB，写满即停记录但继续转发 ——
   长时间测试不能把车机磁盘写满。

输出约定（上位机按这个解析）
---------------------------
    TAP_READY <pty路径>          就绪，可以启 bringup
    TAP_LOG <目录>               日志目录
    TAP_STATS <json>             每 5 秒一次，转发计数
    TAP_ERROR <原因>             出错，上位机应停 bringup
    TAP_EXIT <code>              退出

用法
----
    serial_tap.py --port /dev/ttyUSB0 --baud 115200 --out /tmp/tap
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import pty
import select
import signal
import sys
import time
import tty

try:
    import serial
except ImportError:  # pragma: no cover - 车上一定有，本地开发可能没有
    serial = None

# 每方向日志上限。写满后停止记录，转发不受影响。
MAX_LOG_BYTES = 32 * 1024 * 1024
# 统计打印间隔
STATS_INTERVAL = 5.0
# select 超时。小一点让信号响应及时。
SELECT_TIMEOUT = 0.2


def emit(tag: str, payload: str) -> None:
    """向上位机输出一行。必须立即 flush，否则上位机拿不到 pty 路径。"""
    sys.stdout.write(f"{tag} {payload}\n")
    sys.stdout.flush()


class Tap:
    def __init__(self, port: str, baud: int, out_dir: str,
                 max_log_bytes: int = MAX_LOG_BYTES):
        self.port = port
        self.baud = baud
        self.out_dir = out_dir
        self.max_log_bytes = max_log_bytes

        self.running = True
        self.master_fd = -1
        self.slave_fd = -1
        self.ser = None

        self.log_to_mcu = None
        self.log_from_mcu = None
        self.written_to_mcu = 0
        self.written_from_mcu = 0

        # 转发计数：用于判断「到底有没有数据流过」
        self.count_to_mcu = 0
        self.count_from_mcu = 0
        self.dropped_to_mcu = 0    # pty 没被 open 时收到的数据
        self.slave_is_open = False

    # -- 生命周期 ---------------------------------------------------------

    def setup(self) -> None:
        os.makedirs(self.out_dir, exist_ok=True)

        # 1. 真实串口
        self.ser = serial.Serial(
            port=self.port,
            baudrate=self.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.05,
            write_timeout=0.5,
        )
        self.ser.reset_input_buffer()

        # 2. pty 对
        self.master_fd, self.slave_fd = pty.openpty()
        # 预置 raw，减少底盘节点 open 之前窗口期的输出变形
        try:
            tty.setraw(self.slave_fd)
        except Exception:
            pass
        pty_path = os.ttyname(self.slave_fd)

        # 3. 日志
        self.log_to_mcu = open(os.path.join(self.out_dir, "to_mcu.raw"), "wb")
        self.log_from_mcu = open(os.path.join(self.out_dir, "from_mcu.raw"), "wb")

        meta = {
            "port": self.port,
            "baud": self.baud,
            "pty": pty_path,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "max_log_bytes": self.max_log_bytes,
        }
        with open(os.path.join(self.out_dir, "meta.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)

        emit("TAP_LOG", self.out_dir)
        emit("TAP_READY", pty_path)

    def teardown(self) -> None:
        for fd in (self.master_fd, self.slave_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.master_fd = self.slave_fd = -1
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        for fh in (self.log_to_mcu, self.log_from_mcu):
            if fh:
                try:
                    fh.close()
                except Exception:
                    pass

    def stop(self, *_args) -> None:
        self.running = False

    # -- 转发 -------------------------------------------------------------

    def _record(self, fh, data: bytes, counter_attr: str) -> None:
        """写日志，超上限自动停。转发不受影响。"""
        if fh is None:
            return
        used = getattr(self, counter_attr)
        if used + len(data) > self.max_log_bytes:
            # 只写还装得下的部分，然后关掉这个方向的记录
            room = max(0, self.max_log_bytes - used)
            if room:
                fh.write(data[:room])
                fh.flush()
                setattr(self, counter_attr, used + room)
            if counter_attr == "written_to_mcu":
                self.log_to_mcu = None
            else:
                self.log_from_mcu = None
            emit("TAP_ERROR", f"log_limit_reached:{counter_attr}")
            return
        fh.write(data)
        fh.flush()
        setattr(self, counter_attr, used + len(data))

    def _to_mcu(self) -> None:
        """底盘节点 → 真实串口（下发指令方向）。"""
        try:
            data = os.read(self.master_fd, 4096)
        except OSError as exc:
            if exc.errno == errno.EIO:
                # master 还没有 slave 打开，或者 slave 已关闭。
                # 这不是错误，静默等待。
                return
            raise
        if not data:
            return
        self.count_to_mcu += len(data)
        self._record(self.log_to_mcu, data, "written_to_mcu")
        try:
            self.ser.write(data)
        except Exception as exc:
            emit("TAP_ERROR", f"serial_write_failed:{exc}")

    def _from_mcu(self) -> None:
        """真实串口 → 底盘节点（上报数据方向）。"""
        data = self.ser.read(4096)
        if not data:
            return
        self.count_from_mcu += len(data)
        self._record(self.log_from_mcu, data, "written_from_mcu")
        try:
            os.write(self.master_fd, data)
        except OSError as exc:
            if exc.errno == errno.EIO:
                # 底盘节点还没 open pty。数据丢掉，但要计数 ——
                # 「pty 打开前的帧」是排查启动顺序问题的重要线索。
                self.dropped_to_mcu += len(data)
                return
            raise

    def loop(self) -> int:
        last_stats = time.time()
        while self.running:
            try:
                readable, _, _ = select.select(
                    [self.ser.fileno(), self.master_fd], [], [], SELECT_TIMEOUT
                )
            except (OSError, ValueError) as exc:
                emit("TAP_ERROR", f"select_failed:{exc}")
                return 3

            if self.ser.fileno() in readable:
                self._from_mcu()
            if self.master_fd in readable:
                self._to_mcu()

            now = time.time()
            if now - last_stats >= STATS_INTERVAL:
                last_stats = now
                emit("TAP_STATS", json.dumps({
                    "to_mcu": self.count_to_mcu,
                    "from_mcu": self.count_from_mcu,
                    "dropped_before_open": self.dropped_to_mcu,
                }))
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="串口桥接嗅探器")
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--out", default="/tmp/tap")
    ap.add_argument("--max-log-mb", type=int, default=32)
    args = ap.parse_args()

    if serial is None:
        emit("TAP_ERROR", "pyserial_missing")
        return 2

    tap = Tap(args.port, args.baud, args.out, args.max_log_mb * 1024 * 1024)
    signal.signal(signal.SIGTERM, tap.stop)
    signal.signal(signal.SIGINT, tap.stop)

    try:
        tap.setup()
    except Exception as exc:
        emit("TAP_ERROR", f"setup_failed:{exc}")
        tap.teardown()
        return 2

    try:
        code = tap.loop()
    except Exception as exc:
        emit("TAP_ERROR", f"loop_crashed:{exc}")
        code = 4
    finally:
        tap.teardown()

    emit("TAP_EXIT", str(code))
    return code


if __name__ == "__main__":
    sys.exit(main())
