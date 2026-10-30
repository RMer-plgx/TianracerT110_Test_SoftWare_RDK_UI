"""路径解析：区分「只读资源」和「可写输出」。

PyInstaller 打包后有两套完全不同的路径，混用会出两类故障：

1. 用 __file__ 找 config/report/probes → 打包后指向临时解压目录，
   如果解压目录被清理或路径结构变化，就找不到文件。
   必须用 sys._MEIPASS。

2. 把 test records 写到资源目录 → 打包后写进了临时解压目录，
   程序退出即被删除，使用者永远找不到报告。
   必须基于 exe 自身位置。

本模块是全项目唯一允许计算这两类路径的地方。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包产物里。"""
    return getattr(sys, "frozen", False)


def resource_path(relative: str) -> Path:
    """只读资源（config/、report/、probes/）的绝对路径。

    开发态：相对项目根目录
    打包态：相对 PyInstaller 解压目录（sys._MEIPASS）
    """
    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent.parent
    return base / relative


def app_dir() -> Path:
    """程序「所在」目录，用于放可写输出。

    开发态：项目根目录
    打包态：exe 所在目录（不是解压目录）
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def records_root() -> Path:
    """test records 目录，与程序目录**同级**（不是它内部）。

    开发态：D:\\Tianbot\\产品测试\\TianracerT110_Test_SoftWare_RDK\\..\\test records
    打包态：exe 所在目录的上一级

    目录名含空格，后续拼接必须用 pathlib，不要手动拼命令行。
    """
    return app_dir().parent / "test records"


def _safe_timestamp(moment: Optional[datetime] = None) -> str:
    """生成能直接用作目录名的时间戳。

    Windows 路径不允许冒号，所以不能用 ISO 8601 的
    2026-10-30T17:15:00 形式，必须是 20261030_171500。
    """
    moment = moment or datetime.now()
    return moment.strftime("%Y%m%d_%H%M%S")


def _safe_host(host: str) -> str:
    """把 IP/主机名转成安全的目录名片段。"""
    cleaned = "".join(c if c.isalnum() or c in ".-_" else "_" for c in host)
    return cleaned or "unknown"


def make_record_dir(host: str, moment: Optional[datetime] = None) -> Path:
    """为一次测试创建输出目录并返回。

    目录名格式：<IP>_<YYYYMMDD_HHMMSS>
    若同名目录已存在（同一秒内跑两次），追加序号避免覆盖。
    """
    root = records_root()
    root.mkdir(parents=True, exist_ok=True)
    base_name = f"{_safe_host(host)}_{_safe_timestamp(moment)}"
    candidate = root / base_name
    if not candidate.exists():
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    counter = 2
    while True:
        alternative = root / f"{base_name}_{counter}"
        if not alternative.exists():
            alternative.mkdir(parents=True, exist_ok=True)
            return alternative
        counter += 1


def lock_file_path() -> Path:
    """并发保护用的锁文件，放在程序目录下。"""
    return app_dir() / ".tianracer_test.lock"


def ensure_utf8_env() -> None:
    """Windows 控制台默认 GBK，直接 print 中文以外的符号会抛异常。

    在程序入口调用一次，尽量让输出编码稳定。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
