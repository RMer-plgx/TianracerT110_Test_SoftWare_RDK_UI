"""有线/无线连接模块的测试。

覆盖 2026-11-05 审查（Round 7）报出的两类缺陷：

  M1 命令注入：SSID / 密码未转义就拼进 shell
  M2 中文 Windows 上 host_wifi() 永远返回未连接

M2 的根因值得记住：中文 netsh 输出里有三行都含「状态」
（状态 / 无线电状态 / 承载网络状态），子串匹配会让最后一行赢，
于是连着 WiFi 也被判成断开 —— 有线模式 100% 失败，而用户会
以为是自己的 WiFi 有问题。
"""

import ast
import inspect
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import connect  # noqa: E402
from core.ssh import shquote  # noqa: E402


# ---------------------------------------------------------------------------
# M2：netsh 解析
# ---------------------------------------------------------------------------

# 中文 Windows 的真实输出结构（含那三行都带「状态」的干扰项）
CN_CONNECTED = """系统上有 1 个接口:

    名称                   : WLAN
    描述                   : Realtek 8822CE Wireless LAN 802.11ac PCI-E NIC
    物理地址               : 40:5b:d8:97:2c:67
    接口类型               : 主要
    状态                  : 已连接
    SSID                   : TianbotOffice-5G
    BSSID                  : 28:4d:7d:26:18:cd
    网络类型               : 结构
    无线电类型             : 802.11ac
    身份验证               : WPA2 - 个人
    密码                   : CCMP
    连接模式               : 自动连接
    频带                   : 5 GHz
    信道                   : 48
    信号                   : 100%
    配置文件               : TianbotOffice-5G

    承载网络状态  : 不可用
    无线电状态            : 硬件 开
"""

CN_DISCONNECTED = (
    CN_CONNECTED
    .replace("    状态                  : 已连接\n", "    状态                  : 已断开\n")
    .replace("    SSID                   : TianbotOffice-5G\n", "")
)

EN_CONNECTED = """There is 1 interface on the system:

    Name                   : WLAN
    Description            : Realtek 8822CE
    State                  : connected
    SSID                   : TianbotOffice-5G
    BSSID                  : 28:4d:7d:26:18:cd
    Signal                 : 100%
    Hosted network status  : Not available
"""

EN_DISCONNECTED = """There is 1 interface on the system:

    Name                   : WLAN
    State                  : disconnected
    Hosted network status  : Not available
"""


class TestParseNetsh:
    def test_chinese_connected(self):
        ssid, state, connected = connect.parse_netsh_output(CN_CONNECTED)
        assert connected is True
        assert ssid == "TianbotOffice-5G"
        assert state == "已连接"

    def test_chinese_disconnected(self):
        ssid, _state, connected = connect.parse_netsh_output(CN_DISCONNECTED)
        assert connected is False
        assert ssid == ""

    def test_english_connected(self):
        ssid, _state, connected = connect.parse_netsh_output(EN_CONNECTED)
        assert connected is True
        assert ssid == "TianbotOffice-5G"

    def test_english_disconnected(self):
        ssid, _state, connected = connect.parse_netsh_output(EN_DISCONNECTED)
        assert connected is False
        assert ssid == ""

    def test_radio_status_line_does_not_override(self):
        """「无线电状态 : 硬件 开」不能把「已连接」覆盖掉。

        这正是 Round 7 报的缺陷：三行都含「状态」，最后一行赢。
        """
        ssid, state, connected = connect.parse_netsh_output(CN_CONNECTED)
        assert state == "已连接", f"state 被后续行覆盖了：{state!r}"
        assert connected is True

    def test_disconnected_not_matched_as_connected(self):
        """`disconnected` 里含 `connected`，子串判断会误判。"""
        _ssid, _state, connected = connect.parse_netsh_output(EN_DISCONNECTED)
        assert connected is False

    def test_ssid_with_colon_is_preserved(self):
        """SSID 允许含冒号，不能截断。"""
        out = "    State   : connected\n    SSID    : My:Net:5G\n"
        ssid, _state, connected = connect.parse_netsh_output(out)
        assert ssid == "My:Net:5G"
        assert connected is True

    def test_fullwidth_colon_is_normalised(self):
        """全角冒号也要能解析。

        某些系统/输入法会输出「状态：已连接」。不归一化的话
        会静默判成未连接 —— 和 Round 7 那个 bug 是同一种失败形态。
        """
        out = "    状态：已连接\n    SSID：TestNet\n"
        ssid, state, connected = connect.parse_netsh_output(out)
        assert ssid == "TestNet"
        assert state == "已连接"
        assert connected is True

    def test_mixed_colon_styles(self):
        """全角键 + 半角值（或反之）也要能处理。"""
        out = "    状态: 已连接\n    SSID：TestNet\n"
        ssid, _state, connected = connect.parse_netsh_output(out)
        assert ssid == "TestNet"
        assert connected is True

    def test_empty_output(self):
        ssid, state, connected = connect.parse_netsh_output("")
        assert (ssid, state, connected) == ("", "", False)


# ---------------------------------------------------------------------------
# 编码：netsh 的输出编码随环境变
# ---------------------------------------------------------------------------
# 2026-11-07 实车发现：同一个 netsh，在 UTF-8 代码页的终端里输英文，
# 在无控制台的 windowed 环境（打包后的 exe）里输**中文 GBK**。
# host_wifi 原先把输出硬解为 utf-8，GBK 字节变乱码，
# 「已连接」匹配不上 → connected=False → 有线模式报「读不到电脑当前连接的 WiFi」。
#
# 下面的用例直接给 bytes，堵住这条漏网：
# 原有测试全部直接传 str，所以编码环节一直没被测到。

# 真实的 GBK 字节样本（来自 2026-11-07 在 pythonw 下抓的 netsh 输出结构）
CN_GBK_BYTES = (
    "\r\n系统上有 1 个接口: \r\n\r\n"
    "    名称                   : WLAN\r\n"
    "    说明                   : Realtek 8822CE Wireless LAN 802.11ac PCI-E NIC\r\n"
    "    状态                   : 已连接\r\n"
    "    SSID                   : TianbotOffice-5G\r\n"
    "    无线电状态             : 硬件 开\r\n"
    "    承载网络状态           : 不可用\r\n"
).encode("gbk")

EN_UTF8_BYTES = (
    "\r\nThere is 1 interface on the system: \r\n\r\n"
    "    Name                   : WLAN\r\n"
    "    State                  : connected\r\n"
    "    SSID                   : TianbotOffice-5G\r\n"
).encode("utf-8")


class TestNativeEncoding:
    def test_decode_gbk(self):
        """GBK 字节要能正确解出中文。"""
        text = "    状态                   : 已连接\r\n"
        assert connect._decode_native(text.encode("gbk")) == text

    def test_decode_utf8(self):
        """UTF-8 仍然优先，不能被 gbk 抢掉。"""
        text = "    State                  : connected\r\n"
        assert connect._decode_native(text.encode("utf-8")) == text

    def test_decode_garbage_does_not_raise(self):
        """两种编码都解不开时不能抛异常。"""
        assert isinstance(connect._decode_native(b"\xff\xfe\x00\x01"), str)

    def _patch(self, monkeypatch, raw: bytes):
        class _Result:
            stdout = raw
            stderr = b""
            returncode = 0

        monkeypatch.setattr(connect.subprocess, "run",
                            lambda *a, **k: _Result())

    def test_host_wifi_reads_gbk_output(self, monkeypatch):
        """windowed 环境下 netsh 输出 GBK —— 必须仍判为已连接。

        这是 2026-11-07 有线模式失败的直接原因：
        真实输出 connected=False，而 WiFi 明明是连着的。
        """
        self._patch(monkeypatch, CN_GBK_BYTES)
        wifi = connect.host_wifi()
        assert wifi.connected is True
        assert wifi.ssid == "TianbotOffice-5G"

    def test_host_wifi_reads_utf8_output(self, monkeypatch):
        """英文 UTF-8 输出仍要正常工作（防止改坏）。"""
        self._patch(monkeypatch, EN_UTF8_BYTES)
        wifi = connect.host_wifi()
        assert wifi.connected is True
        assert wifi.ssid == "TianbotOffice-5G"

    def test_host_wifi_disconnected_in_gbk(self, monkeypatch):
        """GBK 的「已断开」仍要判为未连接，不能一律放行。"""
        raw = CN_GBK_BYTES.decode("gbk").replace(
            "状态                   : 已连接",
            "状态                   : 已断开",
        ).encode("gbk")
        self._patch(monkeypatch, raw)
        assert connect.host_wifi().connected is False


# ---------------------------------------------------------------------------
# M1：shell 转义
# ---------------------------------------------------------------------------

class _FakeRemote:
    def __init__(self, stdout=""):
        self.cmds = []
        self._stdout = stdout

    def run(self, cmd, timeout=30, record=True):
        self.cmds.append(cmd)

        class _Out:
            def __init__(self, text):
                self.stdout = text
                self.stderr = ""
                self.ok = True
        return _Out(self._stdout)


class TestNoCommandInjection:
    def test_injected_ssid_is_escaped(self):
        """SSID 是外部可控的（附近任何 AP），必须转义。

        不转义时 `Office'; id; '` 会让 `id` 变成独立命令。
        转义后的文本里仍会有 `id` 这些字符 —— 关键看它是否还在引号保护
        范围内，所以用 shell 词法检查，而不是子串匹配。
        """
        remote = _FakeRemote()
        connect.join_wireless(remote, "Office'; id; '", "pw")

        for cmd in remote.cmds:
            if "nmcli" not in cmd:
                continue
            assert _single_command(cmd), f"命令被注入了：{cmd}"

    def test_injected_password_is_escaped(self):
        remote = _FakeRemote()
        connect.join_wireless(remote, "Normal", "pa'ss'; whoami; '")

        for cmd in remote.cmds:
            if "nmcli" not in cmd:
                continue
            assert _single_command(cmd), f"命令被注入了：{cmd}"

    def test_injected_ssid_keeps_value_intact(self):
        """转义后 SSID 的字面值必须原样传给 nmcli。

        用 shlex 按 shell 规则拆开命令，SSID 应该是独立的一个参数，
        值就是原始字符串（含单引号）。
        """
        import shlex

        remote = _FakeRemote()
        evil = "Office'; id; '"
        connect.join_wireless(remote, evil, "pw")

        for cmd in remote.cmds:
            if "wifi connect" not in cmd:
                continue
            tokens = shlex.split(cmd)
            assert evil in tokens, f"SSID 没被当成一个完整参数：{tokens}"
            break
    def test_normal_values_still_work(self):
        remote = _FakeRemote()
        connect.join_wireless(remote, "TianbotOffice-5G", "pass123")

        joined = "\n".join(remote.cmds)
        assert "'TianbotOffice-5G'" in joined
        assert "'pass123'" in joined

    def test_existing_profile_uses_up_not_connect(self):
        """已有同名连接时只 up，不新建 profile。

        这一点顺带减小了「车上留持久化 WiFi 配置」的影响面。
        """
        remote = _FakeRemote(stdout="TianbotOffice-5G\n")
        connect.join_wireless(remote, "TianbotOffice-5G", "pw")

        joined = "\n".join(remote.cmds)
        assert "nmcli connection up" in joined
        assert "wifi connect" not in joined


class TestShquoteHelper:
    def test_plain_string(self):
        assert shquote("abc") == "'abc'"

    def test_single_quote(self):
        assert shquote("a'b") == "'a'\"'\"'b'"

    def test_dollar_and_backtick_are_inert(self):
        # 单引号内的 $ 与反引号不展开
        assert shquote("$HOME`id`") == "'$HOME`id`'"


# ---------------------------------------------------------------------------
# SSID 匹配（此前已有实现，这里补验收）
# ---------------------------------------------------------------------------

class TestMatchSsid:
    def test_exact_wins(self):
        assert connect.match_ssid(
            "TianbotOffice-5G",
            ["TianbotOffice", "TianbotOffice-5G"]) == "TianbotOffice-5G"

    def test_band_suffix_fallback(self):
        # 电脑在 5G、车在 2.4G 是常态
        assert connect.match_ssid(
            "TianbotOffice-5G", ["TianbotOffice"]) == "TianbotOffice"

    def test_no_guess_on_prefix(self):
        # TianbotOffice 是 TianbotOffice-602 的前缀，但不是同一个网络
        assert connect.match_ssid(
            "TianbotOffice", ["TianbotOffice-602"]) is None

    def test_empty_target(self):
        assert connect.match_ssid("", ["A", "B"]) is None

def _single_command(cmd: str) -> bool:
    """用 shell 词法分析判断命令是否只有一条。

    注入成功后 `; ` 会把命令切成多段 —— shlex 在引号外遇到分号
    就说明这是一条复合命令，判定为不安全。
    （`timeout 60 nmcli ...` 是单条命令的前缀参数，不算注入。）
    """
    import shlex

    # 只查「引号外的分号」—— 那是命令分隔符。
    # 管道符 `|` 不算：`grep -Fx ... || true` 本来就是正常写法，
    # 注入者无法靠管道拿到额外执行权（管道两侧仍是同一条命令行）。
    try:
        lexer = shlex.shlex(cmd, posix=True, punctuation_chars=";")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False

    # 引号内的分号会被 shlex 吃掉，只有裸露的分号才单独成 token
    return not any(t == ";" for t in tokens)


# ---------------------------------------------------------------------------
# 拔线确认：一次短校验，不是后台轮询
# ---------------------------------------------------------------------------

class TestVerifyUnplugged:
    """用户点「已拔线」后的一次短确认。

    2026-11-07 实车反馈：旧实现每 1.5 秒 ping 一次、连轮 60 秒，
    打包后的 exe 里 ping 子进程每个都闪一个黑窗，观感很差。
    改成「用户确认 → 几秒内落地校验」。
    """

    def test_returns_true_immediately_when_unreachable(self, monkeypatch):
        monkeypatch.setattr(connect, "_ping", lambda *a, **k: False)
        started = time.monotonic()
        assert connect.verify_unplugged(grace_seconds=6.0) is True
        # 不可达就立刻返回，不等满窗口
        assert time.monotonic() - started < 1.0

    def test_returns_false_when_still_reachable(self, monkeypatch):
        monkeypatch.setattr(connect, "_ping", lambda *a, **k: True)
        assert connect.verify_unplugged(grace_seconds=0.2, interval=0.05) is False

    def test_tolerates_nic_removal_delay(self, monkeypatch):
        """刚拔线时第一跳往往还能通 —— 窗口内的后续探测要能翻转。"""
        results = iter([True, True, False])
        monkeypatch.setattr(connect, "_ping", lambda *a, **k: next(results))
        assert connect.verify_unplugged(grace_seconds=2.0, interval=0.01) is True


# ---------------------------------------------------------------------------
# 不弹黑窗：所有子进程必须带 CREATE_NO_WINDOW
# ---------------------------------------------------------------------------

def test_no_window_flag_matches_platform():
    import subprocess

    if sys.platform.startswith("win"):
        assert connect._NO_WINDOW == subprocess.CREATE_NO_WINDOW
    else:
        # POSIX 上 creationflags 必须为 0
        assert connect._NO_WINDOW == 0


def test_all_subprocess_calls_hide_console():
    """connect 里每个子进程调用都要带 creationflags。

    打包后是 --windowed exe，没有控制台；子进程默认会各自开一个可见窗口，
    ping 一次闪一个。这条测试防的是以后新增调用时漏掉。
    """
    tree = ast.parse(inspect.getsource(connect))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr in {"run", "Popen", "call", "check_output", "check_call"}
    ]
    assert calls, "connect 里应当有子进程调用"
    for call in calls:
        assert any(kw.arg == "creationflags" for kw in call.keywords), (
            f"core/connect.py:{call.lineno} 的子进程调用没带 creationflags，"
            f"打包后会闪黑窗"
        )


def test_ui_uses_one_shot_check_not_polling():
    """界面侧不得回到长轮询（那是黑窗频闪的根源）。"""
    src = (Path(__file__).resolve().parent.parent / "ui" / "main_window.py").read_text(
        encoding="utf-8"
    )
    assert "verify_unplugged" in src
    assert "wait_for_unplug" not in src


def test_runtime_modules_have_no_unescaped_subprocess():
    """打包产物里的每个子进程调用都要带 creationflags。

    打包后是 `--windowed` exe，没有控制台；子进程默认会各自开一个可见窗口，
    ping 一次闪一个。

    扫描范围从 `build.py` 的 `DATA_DIRS` 与 `CHECK_MODULES` 推导 ——
    **单一来源**。上一版写死 `core/checks/ui`，漏了 `probes/`，
    而它是随 exe 分发的资源（`DATA_DIRS = ["products", "report", "probes", "assets"]`）。

    两类形式盲区一并堵上：
    - `from subprocess import run` 后直接 `run(...)`（通过解析 import 表识别）
    - `creationflags=0` 字面量（Windows 上等于没设，照样闪黑窗）
    """
    root = Path(__file__).resolve().parent.parent
    targets = _packaged_python_files(root)
    assert targets, "推导出的运行时包目录为空，测试本身失效"

    offenders = []
    for path in targets:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        aliases = _subprocess_aliases(tree)
        rel = str(path.relative_to(root)).replace("\\", "/")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _is_subprocess_call(node.func, aliases):
                continue
            flags = [kw.value for kw in node.keywords if kw.arg == "creationflags"]
            if not flags:
                offenders.append(f"{rel}:{node.lineno} 没带 creationflags")
            elif any(isinstance(v, ast.Constant) and v.value == 0 for v in flags):
                # 字面量 0 在 Windows 上不隐藏窗口；该用 _NO_WINDOW
                offenders.append(f"{rel}:{node.lineno} creationflags=0，会闪黑窗")
    assert not offenders, (
        "以下子进程调用会闪黑窗：" + "；".join(offenders)
    )


def _packaged_python_files(root: Path) -> list:
    """从 build.py 推导出会被打包的 Python 文件。

    不写死包名：`build.py` 改了数据目录或检测模块清单时，这里自动跟上。
    """
    import build as build_mod

    files = []
    for pkg in ("core", "checks", "ui"):
        files.extend((root / pkg).glob("*.py"))
    for name in build_mod.DATA_DIRS:
        d = root / name
        if d.is_dir():
            files.extend(d.glob("*.py"))
    return sorted(set(files))


def _subprocess_aliases(tree) -> set:
    """识别 `import subprocess as sp` / `from subprocess import run as r`。"""
    module_aliases = {"subprocess"}
    func_aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "subprocess" and a.asname:
                    module_aliases.add(a.asname)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for a in node.names:
                if a.name in {"run", "Popen", "call", "check_output", "check_call"}:
                    func_aliases.add(a.asname or a.name)
    return {"modules": module_aliases, "funcs": func_aliases}


def _is_subprocess_call(func, aliases) -> bool:
    """是否是 subprocess 的调用（含别名写法）。"""
    attrs = {"run", "Popen", "call", "check_output", "check_call"}
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id in aliases["modules"] and func.attr in attrs
    if isinstance(func, ast.Name):
        return func.id in aliases["funcs"]
    return False

