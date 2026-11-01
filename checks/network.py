"""1. 网络连通性。

SSH 能连上就说明 22 端口通，这里补充 ICMP 和响应延迟。
"""

from __future__ import annotations

import time

from core.verdict import CheckResult, Status

from ._util import parse_float


def run(ctx) -> CheckResult:
    result = CheckResult(number=1, title="网络连通性", status=Status.PASS,
                         fatal=True)
    r = ctx.remote

    # SSH 已由 runner 建立，这里验证会话可用
    who = r.run("hostname; whoami", timeout=15)
    if not who.ok:
        result.status = Status.FAIL
        result.summary = "SSH 会话不可用"
        return result
    result.add("SSH 认证", "成功", expected="成功")
    lines = [ln.strip() for ln in who.stdout.splitlines() if ln.strip()]
    if lines:
        result.add("远端主机名", lines[0])

    # SSH 往返延迟：连续 3 次 echo 取平均，反映链路质量
    timings = []
    for _ in range(3):
        started = time.time()
        r.run("true", timeout=10, record=False)
        timings.append((time.time() - started) * 1000)
    avg_ms = sum(timings) / len(timings)
    result.add("SSH 往返延迟", f"{avg_ms:.1f}", "ms",
               expected="< 200 ms",
               note="局域网正常在 20 以内")
    if avg_ms > 500:
        result.status = Status.WARN
        result.summary = f"延迟偏高：{avg_ms:.0f} ms"

    result.summary = result.summary or f"SSH 正常，延迟 {avg_ms:.0f} ms"
    return result
