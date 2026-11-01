"""SSH 连接与命令执行。

设计约束（来自 2026-10-30 实测）：
- 远端每条命令都必须带 ROS 环境前缀，因为非交互 shell 不会加载 .bashrc
  里末尾的 ros1_setup()，不 source 就找不到 rospy / rostopic。
- 命令里不要用 bash -lc 套引号，容易出现引号嵌套地狱；
  复杂脚本一律 base64 传输后执行。
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Optional

import paramiko


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_sec: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def combined(self) -> str:
        return (self.stdout + ("\n" + self.stderr if self.stderr.strip() else "")).strip()


class SSHError(RuntimeError):
    pass


class Remote:
    """一个到车辆的 SSH 会话。

    用法：
        with Remote("192.168.1.128", "sunrise", "sunrise") as r:
            print(r.run("hostname").stdout)

    所有命令的原始输入输出都会被记录到 self.history，供报告写 raw.log。
    """

    def __init__(self, host: str, user: str, password: str,
                 port: int = 22, timeout: int = 15):
        self.host = host
        self.user = user
        self._password = password
        self.port = port
        self.timeout = timeout
        self._client: Optional[paramiko.SSHClient] = None
        self.history: list[CommandResult] = []

    # -- 生命周期 ---------------------------------------------------------

    def connect(self) -> None:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                self.host, port=self.port, username=self.user,
                password=self._password, timeout=self.timeout,
                allow_agent=False, look_for_keys=False,
            )
        except Exception as exc:  # paramiko 的异常类型很多，统一转换
            raise SSHError(f"无法连接 {self.user}@{self.host}:{self.port} — {exc}") from exc
        self._client = client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "Remote":
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- 执行 -------------------------------------------------------------

    def run(self, command: str, timeout: int = 60,
            record: bool = True) -> CommandResult:
        """执行一条 bash 命令，返回结果。

    超时/断线时**不抛异常**，返回 exit_code=-1 和 stderr 里的错误描述。
    调用方统一看 exit_code，不用写满 try/except。
    """
        if self._client is None:
            raise SSHError("会话未建立，先调用 connect()")

        started = time.time()
        try:
            _stdin, stdout, stderr = self._client.exec_command(
                f"bash -c {_shquote(command)}", timeout=timeout
            )
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            code = stdout.channel.recv_exit_status()
        except Exception as exc:
            # socket.timeout / paramiko.SSHException / EOFError / PipeTimeout
            # 统一转成可读的失败，不让上层抛到 runner 导致整轮中止
            out = ""
            err = f"[远程执行失败] {type(exc).__name__}: {exc or '无详细信息'}"
            code = -1

        result = CommandResult(
            command=command, exit_code=code, stdout=out, stderr=err,
            duration_sec=time.time() - started,
        )
        if record:
            self.history.append(result)
        return result

    def run_script(self, script: str, timeout: int = 120,
                   interpreter: str = "python3") -> CommandResult:
        """把一段脚本 base64 后送到车上执行，用完即删。

        这样不需要在车辆上留下任何文件，也不用手动 scp。
        脚本内容里的引号、中文、换行都不会出问题。
        """
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        cmd = (
            f"echo {encoded} | base64 -d > /tmp/_tt_probe.py && "
            f"{interpreter} /tmp/_tt_probe.py; "
            f"rc=$?; rm -f /tmp/_tt_probe.py; exit $rc"
        )
        return self.run(cmd, timeout=timeout)

    def put_script(self, script: str, remote_path: str) -> None:
        """把脚本写到车上指定位置（用于需要多次调用的探针）。"""
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        self.run(f"echo {encoded} | base64 -d > {remote_path}", record=False)

    def sudo(self, command: str, sudo_password: str,
             timeout: int = 60) -> CommandResult:
        """带密码的 sudo 执行。"""
        escaped = _shquote(command)
        cmd = f"echo {_shquote(sudo_password)} | sudo -S bash -c {escaped}"
        return self.run(cmd, timeout=timeout)

    # -- 辅助 -------------------------------------------------------------

    def read_file(self, path: str) -> str:
        result = self.run(f"cat {path} 2>/dev/null", record=False)
        return result.stdout

    def exists(self, path: str) -> bool:
        return self.run(f"test -e {path}", record=False).exit_code == 0


def _shquote(text: str) -> str:
    """POSIX 安全的单引号转义。"""
    return "'" + text.replace("'", "'\"'\"'") + "'"


def probe_ssh(host: str, port: int = 22, timeout: float = 3.0) -> bool:
    """只探测 22 端口是否开放，不做认证。用于连通性检测的第一层。"""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()
