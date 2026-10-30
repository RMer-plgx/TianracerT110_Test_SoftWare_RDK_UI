"""检测项共用的解析与执行工具。

放这里的都是「多个 check 都要用、且不涉及具体业务判定」的东西。
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Dict, List, Optional

from core.paths import resource_path
from core.remote import ros


def parse_float(text: str) -> Optional[float]:
    """从一段文本里抠出第一个浮点数。抠不到返回 None，而不是 0。

    返回 0 会让调用方误以为「测到了且值为 0」，这是漏报的常见来源。
    """
    match = re.search(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", text or "")
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def parse_key_value(text: str, separator: str = "=") -> Dict[str, str]:
    """把 key=value 或多行 key: value 解析成字典。"""
    result: Dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or separator not in line:
            continue
        key, _, value = line.partition(separator)
        result[key.strip()] = value.strip()
    return result


def measure_hz(ctx, topic: str, seconds: int = 5) -> Optional[float]:
    """测话题频率。

    用 timeout 包住 rostopic hz，无消息时会返回 None，
    与「频率为 0」区分开——前者是没测到，后者是测到了但不动。
    """
    result = ctx.remote.run(
        ros(f"timeout {seconds} rostopic hz {topic} 2>&1 | tail -3"),
        timeout=seconds + 15,
    )
    match = re.search(r"average rate:\s*([\d.]+)", result.stdout)
    if match:
        return float(match.group(1))
    return None


def echo_once(ctx, topic: str, field: str = "", timeout: int = 12) -> Optional[str]:
    """取一条话题消息（或其中某个字段）的值。

    `rostopic echo` 的输出会带尾部 `---` 分隔符，且字符串值带双引号。
    这里统一清洗成纯值，否则 encoding 会变成 'bgr8"\n---'。
    """
    target = f"{topic}/{field}" if field else topic
    result = ctx.remote.run(
        ros(f"timeout {timeout} rostopic echo -n 1 {target} 2>&1"),
        timeout=timeout + 10,
    )
    text = result.stdout.strip()
    if not text or "does not appear to be published" in text:
        return None

    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or candidate == "---":
            continue
        # 字符串字段会带引号："bgr8" → bgr8
        if len(candidate) >= 2 and candidate[0] == '"' and candidate[-1] == '"':
            return candidate[1:-1]
        return candidate
    return None


def run_probe(ctx, probe_name: str, *args: str, timeout: int = 90,
              needs_ros: bool = True) -> Optional[dict]:
    """把 probes/<name>.py 送到车上执行，解析它输出的 JSON。

    探针统一用 `RESULT {json}` 单行输出，避免和 ROS 的日志混在一起。
    """
    source = resource_path(f"probes/{probe_name}.py").read_text(encoding="utf-8")
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    arg_text = " ".join(args)
    cmd = (
        f"echo {encoded} | base64 -d > /tmp/_tt_probe.py && "
        f"python3 /tmp/_tt_probe.py {arg_text} 2>&1; "
        f"rc=$?; rm -f /tmp/_tt_probe.py; exit $rc"
    )
    if needs_ros:
        cmd = ros(cmd)
    result = ctx.remote.run(cmd, timeout=timeout)
    return _extract_json(result.stdout)


def _extract_json(text: str) -> Optional[dict]:
    """从可能混杂 ROS 日志的输出里找出 RESULT 行。"""
    for line in reversed((text or "").splitlines()):
        stripped = line.strip()
        if stripped.startswith("RESULT "):
            try:
                return json.loads(stripped[len("RESULT "):])
            except json.JSONDecodeError:
                continue
    return None


def count_lines(text: str) -> int:
    return len([ln for ln in (text or "").splitlines() if ln.strip()])


def as_list(text: str) -> List[str]:
    """把多行输出转成去空白的列表。"""
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def first_present(values: List[Any]) -> Any:
    """返回第一个非 None 的值。"""
    for value in values:
        if value is not None:
            return value
    return None
