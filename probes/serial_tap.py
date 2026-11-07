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
2. **转发路径不允许阻塞**。master 端写入是非阻塞的，写不完进队列，
   由 select 的可写事件继续冲。否则队列满时循环会卡死，而上位机只能
   看到「进程还在」—— 卡死反而不可见。.
3. **串口写失败立即退出**。写不出去意味着底盘节点以为指令发了、
   其实没到 MCU。继续循环会让「停车」这类指令静默丢失。
4. **退出即不一定安全**（⚠ 重要，旧版本这里写错了）。
   底盘节点持有自己的 slave fd；本进程死掉后它**不会**立刻写失败，
   已发出的运动指令也不会自动撤销，停车靠 MCU 固件的通信超时保护。

   该保护已于 2026-11-07 两次架空实验确认（低速前进 → kill -9 桥接 →
   直接读 MCU 原始帧，轮速与位移都在秒级内停住）。
   但这**是固件特性而不是协议保证**，换车型或固件必须重做实验。因此：
   - 采集期间**车辆必须架空且不得运动**，这是硬约束，不因上述结论放宽；
   - 上位机除监听 `TAP_EXIT` 外，还必须监视 `TAP_STATS` 是否在刷新
     （进程活着但循环停摆的情况只能靠这个发现）；
   - 监视周期的长短只影响发现速度，不改变「MCU 已经停车」这一事实。
5. **写日志有上限**。默认每方向 32 MB，写满即停记录但继续转发 ——
   长时间测试不能把车机磁盘写满。
6. **stdout 不阻塞**。上位机若停止读取，管道满后 print 会卡住整个循环；
   这里改为非阻塞写，统计行可丢，关键行（READY/ERROR/EXIT）短暂重试。

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
# slave 尚未打开时的重试间隔。避免 master 持续 EIO 把 CPU 跑满。
PTY_RETRY_INTERVAL = 0.2
# from_mcu 积压上限。底盘节点不读 pty 时不能把内存涨爆。
MAX_PENDING_BYTES = 1024 * 1024
# 关键消息（READY/LOG/ERROR/EXIT）在管道拥塞时的最长重试时间。
EMIT_RETRY_SEC = 1.0

STDOUT_FD = 1

# 关键消息不能丢；统计行丢了不影响判定。
CRITICAL_TAGS = frozenset({"TAP_READY", "TAP_LOG", "TAP_ERROR", "TAP_EXIT"})


class TapFatal(Exception):
    """不可继续的故障。抛出后必须以非零码退出。"""

    def __init__(self, reason: str, code: int):
        super().__init__(reason)
        self.reason = reason
        self.code = code


def emit(tag: str, payload: str) -> bool:
    """向上位机输出一行。非阻塞，避免管道满时拖停转发循环。

    返回 True 表示写成功。统计行写不出去就丢掉并返回 False ——
    那只是诊断信息，不能因为它把控制链路拖死。
    """
    line = f"{tag} {payload}\n".encode("utf-8", "replace")
    critical = tag in CRITICAL_TAGS
    deadline = time.time() + EMIT_RETRY_SEC
    while True:
        try:
            os.write(STDOUT_FD, line)
            return True
        except BlockingIOError:
            if not critical or time.time() > deadline:
                return False
            time.sleep(0.005)
        except OSError:
            return False


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
        # from_mcu 方向因积压超过上限而丢弃的字节数。
        # （不是「pty 未打开就丢掉」—— 实测缓冲区会先吞下，写成功。）
        self.pending_dropped = 0
        # 待写入 master 的积压数据。非阻塞写的出口。
        self.pending_from_mcu = b""
        # 底盘节点是否已经连过 pty。用来区分「还没启动」和「退出了」。
        self.slave_connected = False
        # slave 未打开时的下次重试时刻，避免 EIO 忙等。
        self.pty_retry_at = 0.0

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

        # master 必须非阻塞。否则底盘节点读得慢时，pty 队列满会让
        # os.write 阻塞，整个 select 循环停摆 —— 而上位机只看到进程还在，
        # 根本发现不了卡死。
        os.set_blocking(self.master_fd, False)

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

        # 3. 交出自己的 slave fd。
        # 必须关，否则 slave 永远处于「已打开」状态，**读**方向就永远
        # 拿不到 EIO —— 「底盘节点到底连没连上」这个判断就失效了。
        # （写方向不受影响：实测写 master 在无 slave 时照样成功，
        #   数据进内核缓冲，见 _on_pty_eio 的注释。）
        # 关闭后 /dev/pts/N 路径仍然存在，底盘节点照样能 open。
        os.close(self.slave_fd)
        self.slave_fd = -1

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
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno == errno.EIO:
                self._on_pty_eio()
                return
            raise
        if not data:
            return
        if not self.slave_connected:
            # 第一次读到数据，说明底盘节点已经连上 pty。
            self.slave_connected = True
        self.count_to_mcu += len(data)
        self._record(self.log_to_mcu, data, "written_to_mcu")
        # 写不出去就是真故障：底盘节点以为指令发了，实际没到 MCU。
        # 若这条指令是「停车」，继续循环等于让车带着旧指令跑。
        try:
            self.ser.write(data)
        except Exception as exc:
            raise TapFatal(f"serial_write_failed:{exc}", 5) from exc

    def _on_pty_eio(self) -> None:
        """master 读到 EIO：对端没有打开 / 已关闭。

        注意：**写方向不会 EIO**（实车 Linux 实测：slave 未打开时写 master
        照样成功，数据进内核缓冲，slave 后来打开还能读到）。所以这个
        判断只靠读方向。
        """
        if self.slave_connected:
            raise TapFatal("slave_closed", 6)
        self.pty_retry_at = time.time() + PTY_RETRY_INTERVAL

    def _on_pty_hup(self) -> None:
        """poll 报 POLLHUP：当前没有 slave 打开。

        仅靠「读到数据」判断 slave 在不在不够 —— 底盘节点可能开了 pty
        但一时没写东西，此时关闭它我们就完全察觉不到。
        POLLHUP 能补上这个洞：实车实测「slave 从未打开」与「打开后又关闭」
        都会报 HUP，而**打开着就不报**，所以「本轮没 HUP」本身就是
        「对端在」的证据。

        注意不能先把 slave_connected 置 False 再判断 —— 那样「连过又断开」
        会被当成「从未连过」，永远退不出去。
        """
        if self.slave_connected:
            raise TapFatal("slave_closed", 6)
        self.pty_retry_at = time.time() + PTY_RETRY_INTERVAL

    def _flush_pending(self) -> None:
        """把积压的 from_mcu 数据尽量写进 pty，能写多少算多少。"""
        while self.pending_from_mcu:
            try:
                written = os.write(self.master_fd, self.pending_from_mcu)
            except BlockingIOError:
                return                      # 队列满了，等下次可写事件
            except OSError as exc:
                if exc.errno == errno.EIO:
                    # 写方向理论上不会 EIO（实测不报），保这个分支是
                    # 为了兼容其他内核/pty 实现。真到了这里就把积压丢掉，
                    # 并计入 pending_dropped。
                    self.pending_dropped += len(self.pending_from_mcu)
                    self.pending_from_mcu = b""
                    self._on_pty_eio()
                    return
                raise
            if written <= 0:
                return
            # 不要在写成功时置 slave_connected：写 master 在没有 slave 时
            # 也会成功（数据进内核缓冲），根本不能证明对端在读。
            self.pending_from_mcu = self.pending_from_mcu[written:]

    def _from_mcu(self) -> None:
        """真实串口 → 底盘节点（上报数据方向）。

        绝不阻塞：写不完的进队列，由 select 的可写事件继续冲。
        """
        data = self.ser.read(4096)
        if not data:
            return
        self.count_from_mcu += len(data)
        self._record(self.log_from_mcu, data, "written_from_mcu")
        self.pending_from_mcu += data
        # 积压太多说明 pty 缓冲已经满（底盘节点没启动 / 不读）。
        # 内存不能无限涨；丢旧的、保留新的。
        if len(self.pending_from_mcu) > MAX_PENDING_BYTES:
            overflow = len(self.pending_from_mcu) - MAX_PENDING_BYTES
            self.pending_from_mcu = self.pending_from_mcu[overflow:]
            self.pending_dropped += overflow
        self._flush_pending()

    def loop(self) -> int:
        last_stats = time.time()
        poller = select.poll()
        poller.register(self.ser.fileno(), select.POLLIN)
        master_watched = False
        # 只在有积压时监听 POLLOUT。pty master 几乎总是可写，
        # 无条件注册会让 poll 立即返回、循环变成忙等。
        out_watched = False
        base_mask = select.POLLIN | select.POLLHUP | select.POLLERR
        while self.running:
            now = time.time()
            want_master = now >= self.pty_retry_at
            if want_master and not master_watched:
                poller.register(self.master_fd, base_mask)
                master_watched = True
            elif not want_master and master_watched:
                poller.unregister(self.master_fd)
                master_watched = False
                out_watched = False

            # 积压 → 加上可写事件，让队列一排空就继续写，
            # 不用干等 0.2 秒的轮询周期。
            want_out = master_watched and bool(self.pending_from_mcu)
            if want_out != out_watched:
                poller.modify(self.master_fd,
                              base_mask | (select.POLLOUT if want_out else 0))
                out_watched = want_out

            if want_master:
                timeout_ms = int(SELECT_TIMEOUT * 1000)
            else:
                remain = max(0.0, self.pty_retry_at - now)
                timeout_ms = int(min(SELECT_TIMEOUT, remain) * 1000)

            try:
                events = poller.poll(timeout_ms)
            except (OSError, ValueError) as exc:
                emit("TAP_ERROR", f"poll_failed:{exc}")
                return 3

            hup = False
            for fd, ev in events:
                if fd == self.ser.fileno():
                    self._from_mcu()
                elif fd == self.master_fd:
                    if ev & (select.POLLHUP | select.POLLERR):
                        hup = True
                    if ev & select.POLLIN:
                        self._to_mcu()
                    if ev & select.POLLOUT:
                        self._flush_pending()

            if want_master:
                if hup:
                    self._on_pty_hup()
                else:
                    # 本轮 poll 没有 HUP —— slave 是打开的。
                    # 这也覆盖了「底盘节点开了 pty 但从不写」的情形，
                    # 那是靠「读到数据」永远发现不了的。
                    self.slave_connected = True
                    self._flush_pending()

            now = time.time()
            if now - last_stats >= STATS_INTERVAL:
                last_stats = now
                emit("TAP_STATS", json.dumps({
                    "to_mcu": self.count_to_mcu,
                    "from_mcu": self.count_from_mcu,
                    "pending_dropped": self.pending_dropped,
                    "pending": len(self.pending_from_mcu),
                    "slave_connected": self.slave_connected,
                }))
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="串口桥接嗅探器")
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--out", default="/tmp/tap")
    ap.add_argument("--max-log-mb", type=int, default=32)
    args = ap.parse_args()

    # stdout 非阻塞：上位机若停止读取，管道满后 print 会卡死整个转发循环。
    try:
        os.set_blocking(STDOUT_FD, False)
    except OSError:
        pass

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
    except TapFatal as exc:
        # 串口写失败 / 底盘节点已断开 —— 继续转发没有意义，
        # 必须让上位机知道并停 bringup。
        emit("TAP_ERROR", exc.reason)
        code = exc.code
    except Exception as exc:
        emit("TAP_ERROR", f"loop_crashed:{exc}")
        code = 4
    finally:
        tap.teardown()

    emit("TAP_EXIT", str(code))
    return code


if __name__ == "__main__":
    sys.exit(main())
