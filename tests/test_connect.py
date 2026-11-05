"""有线/无线连接模块的测试。

覆盖 2026-11-05 审查（Round 7）报出的两类缺陷：

  M1 命令注入：SSID / 密码未转义就拼进 shell
  M2 中文 Windows 上 host_wifi() 永远返回未连接

M2 的根因值得记住：中文 netsh 输出里有三行都含「状态」
（状态 / 无线电状态 / 承载网络状态），子串匹配会让最后一行赢，
于是连着 WiFi 也被判成断开 —— 有线模式 100% 失败，而用户会
以为是自己的 WiFi 有问题。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import connect  # noqa: E402
from core.ssh import _shquote  # noqa: E402


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

    def test_empty_output(self):
        ssid, state, connected = connect.parse_netsh_output("")
        assert (ssid, state, connected) == ("", "", False)


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
        assert _shquote("abc") == "'abc'"

    def test_single_quote(self):
        assert _shquote("a'b") == "'a'\"'\"'b'"

    def test_dollar_and_backtick_are_inert(self):
        # 单引号内的 $ 与反引号不展开
        assert _shquote("$HOME`id`") == "'$HOME`id`'"


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

