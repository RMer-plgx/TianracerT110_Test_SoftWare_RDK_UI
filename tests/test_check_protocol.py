"""第 20 项「通讯链路（协议层）」的判定测试。

解析器本身由 `test_protocol.py` 覆盖（帧长、bcc、半帧、粘包）。
这里只测**判定逻辑**：拿到什么日志，就得出什么结论。

重点防一处已发生的错误结论：
源码里没有电压查询调用，就被推断成「下位机永不发电压」。
2026-11-06 实车证明下位机会主动低频上报（20 秒 3 帧）。
所以「上行没查询」绝不等于「下行不会有回传」，短窗口没捕到也不能判死。
"""

import base64
import struct
from types import SimpleNamespace

import pytest

from checks import protocol as check
from core.profile import TapConfig
from core.protocol import build_frame
from core.verdict import Status

VOLTAGE = 0xC001
IMU = 0x8003
ODOM = 0x8000


class _Resp:
    def __init__(self, stdout: str = ""):
        self.stdout = stdout
        self.stderr = ""
        self.ok = True


class _TapRemote:
    """把 tap 日志文件当作 base64 回给 _fetch 的假远端。

    `_fetch` 先发 `test -s <path>` 判断文件在不在，再取内容，
    所以夹具要同时模拟这两步，否则会被当成「传输失败」。
    """

    host = "127.0.0.1"
    user = "test"

    def __init__(self, files, unreachable=False, present_empty=()):
        self.files = files
        self.unreachable = unreachable      # 模拟 SSH 不可达
        self.present_empty = set(present_empty)  # 模拟存在但为空
        self.commands = []

    def _target(self, cmd):
        for name in self.files:
            if name in cmd:
                return name
        for name in self.present_empty:
            if name in cmd:
                return name
        return None

    def run(self, cmd, timeout=30, record=True):
        self.commands.append(cmd)
        if self.unreachable:
            return _Resp("")
        name = self._target(cmd)
        if name is None:
            return _Resp("missing")
        # _fetch 现在用一条合并命令同时判断存在与空
        if "-e " in cmd and "nonempty" in cmd:
            if name in self.files:
                return _Resp("nonempty" if self.files[name] else "empty")
            return _Resp("empty" if name in self.present_empty else "missing")
        if cmd.startswith("test -s"):
            return _Resp("yes" if self.files.get(name) else "no")
        if cmd.startswith("test -e"):
            return _Resp("yes")
        data = self.files.get(name, b"")
        return _Resp(base64.b64encode(data).decode())

    def close(self):
        pass


class _Ctx:
    """第 20 项的上下文替身。

    默认 tap.enabled=True —— 大多数用例测的是「有数据时怎么判」。
    专门验开关行为的用例自己传 enabled=False。
    """

    def __init__(self, files, enabled=True, log_dir="/tmp/tap", **kwargs):
        self.remote = _TapRemote(files, **kwargs)
        self.tap_dir = log_dir
        self.profile = SimpleNamespace(
            ros=SimpleNamespace(
                tap=TapConfig(enabled=enabled, log_dir=log_dir)
            )
        )


def _odom(value=0.0):
    return build_frame(ODOM, struct.pack("<10f", *([value] * 10)))


def _imu(value=0.0):
    return build_frame(IMU, struct.pack("<10f", *([value] * 10)))


def _voltage(value: float):
    return build_frame(VOLTAGE, struct.pack("<f", value))


def _run(files, **kwargs):
    return check.run(_Ctx(files, **kwargs))


def _names(result):
    return [m.name for m in result.measurements]


def _measure(result, name):
    return next(m for m in result.measurements if m.name == name)


# ---------------------------------------------------------------------------
# 采集未启用
# ---------------------------------------------------------------------------

def test_no_tap_log_gives_skip():
    result = _run({})
    assert result.status is Status.SKIP
    assert result.number == 20


# ---------------------------------------------------------------------------
# 链路正常
# ---------------------------------------------------------------------------

def test_healthy_link_passes_and_reports_voltage():
    down = _odom() + _imu() + _voltage(11.71) + _voltage(11.68)
    result = _run({"from_mcu.raw": down})

    assert result.status is Status.PASS
    assert "bcc 错误 0" in result.summary
    volts = _measure(result, "电压回传")
    assert volts.value == "11.68~11.71"
    assert volts.unit == "V"
    # 报告帧数与类型数，便于判断「有没有在发」
    assert "下行帧数" in _names(result)


def test_link_without_voltage_frames_still_passes():
    """没捕到电压帧不影响链路判定 —— 链路是活的。"""
    result = _run({"from_mcu.raw": _odom() + _imu()})
    assert result.status is Status.PASS


# ---------------------------------------------------------------------------
# 电压诊断：不能把「没捕到」说成「从不会发」
# ---------------------------------------------------------------------------

def test_missing_voltage_without_query_does_not_claim_request_response():
    """上行没有电压查询时，不能断言「不发请求就不会有回传」。

    实车证明下位机主动低频上报，这条断言会在短窗口下变成假结论。
    """
    result = _run({"from_mcu.raw": _odom() + _imu(), "to_mcu.raw": b""})

    note = _measure(result, "电压诊断").note
    assert "不等于不回传" in note
    assert "不发请求就不会有回传" not in note


def test_missing_voltage_with_query_reports_no_response():
    """上位机确实发过电压查询，才能判「下位机没响应」。"""
    up = _voltage(0.0)  # 同样用 0xC001 作为查询类型
    result = _run({"from_mcu.raw": _odom(), "to_mcu.raw": up})

    diag = _measure(result, "电压诊断")
    assert diag.value == "已发查询但本窗口内无回传"


# ---------------------------------------------------------------------------
# 故障判定
# ---------------------------------------------------------------------------

def test_bad_bcc_ratio_fails():
    good = _odom()
    bad = bytearray(good)
    bad[-1] ^= 0xFF  # 破坏 bcc
    down = good + bytes(bad) * 3

    result = _run({"from_mcu.raw": down})
    assert result.status is Status.FAIL
    assert "bcc" in result.summary


def test_undecodable_downlink_fails():
    """有字节但一帧都解析不出 —— 是格式/波特率问题，不是「没数据」。"""
    result = _run({"from_mcu.raw": b"\x00" * 256})
    assert result.status is Status.FAIL
    assert "无任何可解析回传帧" in result.summary


def test_mixed_errors_under_limit_still_pass():
    """偶发噪声（1 帧错 / 大量正常）在 1% 容忍内应判 PASS。"""
    down = _odom() * 200
    bad = bytearray(_odom())
    bad[-1] ^= 0xFF
    result = _run({"from_mcu.raw": down + bytes(bad)})

    assert result.status is Status.PASS
    assert _measure(result, "bcc 错误").value == "1"


# ---------------------------------------------------------------------------
# M1：旧数据不能被当成本次结果
# ---------------------------------------------------------------------------

def test_stale_tap_dir_is_not_reported_as_pass():
    """开关关闭时，即使 /tmp/tap 里躺着健康的旧数据，也必须判 SKIP。

    这是 Round 10 报的「报告说谎」缺陷：tap_probe 只在采集前清目录，
    残留数据会让第 20 项在每次正常检测里都报「链路正常」。
    只看文件在不在决定判决是错的。
    """
    healthy = _odom() * 10 + _voltage(11.65) * 2
    result = _run({"from_mcu.raw": healthy, "to_mcu.raw": b"\x00" * 7},
                  enabled=False)

    assert result.status is Status.SKIP
    assert "未启用" in result.summary
    # 关键：不能把旧数据写进报告
    assert result.measurements == []
    assert "链路正常" not in result.summary


def test_disabled_switch_does_not_even_read_the_files():
    """开关关闭时不应去车上看日志 —— 连一次取数都不发。

    除了正确性，这也是性能：runtime 阶段末尾不必多跑两条命令。
    """
    ctx = _Ctx({"from_mcu.raw": _odom()}, enabled=False)
    check.run(ctx)

    assert not any("-e " in c for c in ctx.remote.commands), ctx.remote.commands


def test_enabled_but_no_data_is_skip_not_pass():
    """开关开了但车上没数据：SKIP（未验证），不能是 PASS。"""
    result = _run({})

    assert result.status is Status.SKIP
    assert "没有数据" in result.summary


def test_log_dir_comes_from_profile_not_hardcoded():
    """日志目录要从 profile 取，不能写死 /tmp/tap。"""
    ctx = _Ctx({"from_mcu.raw": _odom()}, log_dir="/var/tmp/custom_tap")
    check.run(ctx)

    probes = [c for c in ctx.remote.commands if "-e " in c and "nonempty" in c]
    assert probes, "没发出取数探测"
    assert all("/var/tmp/custom_tap" in c for c in probes), probes
    assert not any("/tmp/tap" in c for c in probes), probes


def test_log_path_is_shell_quoted():
    """路径必须被引号包裹。

    `log_dir` 来自 profile，`core/profile.py` 只校验了「必须是绝对路径」，
    没禁空格。带空格的路径不引号会被 shell 拆成多个参数：
    `head -c N /tmp/my tap/from_mcu.raw` → 读两个文件。
    """
    ctx = _Ctx({"from_mcu.raw": _odom()}, log_dir="/tmp/my tap")
    check.run(ctx)

    probes = [c for c in ctx.remote.commands if "-e " in c and "nonempty" in c]
    assert probes, "没发出取数探测"
    for cmd in probes:
        # 带空格的路径必须以单引号形式出现，否则会被 shell 拆开。
        # 两个方向（to_mcu / from_mcu）都取，不能只断言其中一个。
        assert "'/tmp/my tap/" in cmd, cmd
        assert " -e /tmp/my tap/" not in cmd, f"未加引号: {cmd}"

    fetches = [c for c in ctx.remote.commands if "head -c" in c]
    assert fetches, "没发出取数命令"
    for cmd in fetches:
        assert "'/tmp/my tap/" in cmd, cmd


# ---------------------------------------------------------------------------
# 传输层故障不能被当成「未启用采集」
# ---------------------------------------------------------------------------

def test_unreachable_ssh_is_fail_not_skip():
    """SSH 连不上时不能判 SKIP —— 那会把一次故障藏起来。"""
    result = _run({"from_mcu.raw": _odom()}, unreachable=True)

    assert result.status is Status.FAIL
    assert "取回失败" in result.summary


def test_present_but_empty_logs_are_skip():
    """文件在、但两个方向都是 0 字节 —— 真正的「没采到数据」。"""
    result = _run({}, present_empty=("from_mcu.raw", "to_mcu.raw"))

    assert result.status is Status.SKIP


# ---------------------------------------------------------------------------
# 上行空白要报出来
# ---------------------------------------------------------------------------

def test_downlink_only_flags_missing_uplink():
    """下行有帧、上行完全空白：链路活着，但底盘节点可能没接到 pty。"""
    result = _run({"from_mcu.raw": _odom() + _imu()})

    assert result.status is Status.PASS
    assert "上行告警" in _names(result)
    assert "上行无可解析帧" in result.summary


def test_both_directions_healthy_has_no_uplink_warning():
    result = _run({"from_mcu.raw": _odom() + _imu(),
                   "to_mcu.raw": _odom()})

    assert result.status is Status.PASS
    assert "上行告警" not in _names(result)
    assert "上行无可解析帧" not in result.summary


# ---------------------------------------------------------------------------
# 报告可读性
# ---------------------------------------------------------------------------

def test_type_breakdown_is_compact_not_raw_dict():
    """类型分布要拼成短文本，不能把整个 dict 塑进 note。"""
    result = _run({"from_mcu.raw": _odom() + _imu() + _voltage(11.7)})
    note = _measure(result, "下行帧数").note

    assert "{" not in note and "'" not in note
    assert "ODOM_RESPONSE×" in note
    assert len(note) <= 80


# ---------------------------------------------------------------------------
# SKIP 文案必须指向真实存在的东西（Round 13 审查 R13-1）
# ---------------------------------------------------------------------------
#
# 曾经的缺陷：补丁从 `tianracer_core-serial-tap.patch` 改成
# `tianbot_core-01-raw-mode.patch` + `tianbot_core-02-serial-tap.patch`，
# 环境变量从 `TIANRACER_TAP_DIR` 改成 `TIANBOT_TAP_DIR`，但 SKIP 文案没跟上。
# 而这是**全仓唯一一处**告诉用户怎么开启第 20 项的地方 —— 用户照做
# `patch` 找不到文件、环境变量设了不生效，只能认为软件坏了。
#
# 当时 433 条测试没有一条覆盖这个方向，所以它能一直错下去。


class TestSkipHintIsActionable:
    """SKIP detail 里的每个具体指引都必须在仓库里成立。"""

    @staticmethod
    def _repo_root():
        from pathlib import Path
        return Path(__file__).resolve().parent.parent

    def test_patch_dir_has_numbered_patches_in_order(self):
        """补丁目录必须有 01、02 且按编号顺序，文案说的「01 → 02」才成立。"""
        d = self._repo_root() / check.TAP_PATCH_DIR
        assert d.is_dir(), f"{check.TAP_PATCH_DIR} 不存在"
        patches = sorted(p.name for p in d.glob("*.patch"))
        assert patches, f"{check.TAP_PATCH_DIR} 下没有补丁"
        # 文件名形如 tianbot_core-01-raw-mode.patch，编号在中间
        assert "-01-" in patches[0] or patches[0].startswith("01"), \
            f"首个补丁编号不是 01：{patches[0]}"
        if len(patches) > 1:
            assert "-02-" in patches[1] or patches[1].startswith("02"), \
                f"第二个补丁编号不是 02：{patches[1]}"

    def test_env_var_name_matches_patch_and_readme(self):
        """环境变量名要在补丁本体与 README 里一致。

        只改 Python 常量而没改车端补丁，用户设了变量也不会生效。
        """
        root = self._repo_root()
        var = check.TAP_ENV_VAR

        readme = (root / check.TAP_PATCH_DIR / "README.md").read_text(encoding="utf-8")
        assert var in readme, f"{check.TAP_PATCH_DIR}/README.md 没提到 {var}"

        # 车端补丁里应出现同一个名字（getenv 的参数）
        patched = False
        for p in (root / check.TAP_PATCH_DIR).glob("*.patch"):
            text = p.read_text(encoding="utf-8", errors="replace")
            if f'getenv("{var}")' in text:
                patched = True
                break
        assert patched, f"没有任何补丁读取 ${var}"

    def test_old_names_are_gone(self):
        """旧名不该残留在用户可见文案里。"""
        detail = _run({}, enabled=False).detail or ""
        assert "TIANRACER_TAP_DIR" not in detail, "文案还在用旧变量名"
        assert "tianracer_core-serial-tap" not in detail, "文案还在引用不存在的补丁"

    def test_skip_detail_mentions_correct_package(self):
        """必须说清补丁装在 tianbot_core，不是 tianracer_core。"""
        detail = _run({}, enabled=False).detail or ""
        assert "tianbot_core" in detail
        assert "-p1" in detail, "没提醒用 -p1，用户可能用 -p0 直接失败"

    def test_detail_survives_missing_patch_dir(self):
        """打包产物可能不带 docs/。文案生成不能因此抛异常。"""
        result = _run({}, enabled=False)
        assert result.status is Status.SKIP
        assert result.detail  # 非空


    def test_docs_do_not_present_old_var_name_as_current(self):
        """docs/ 下不得把旧变量名当现行用法写。

        只在明确的历史叙述（「以前 / 曾 / 改成 / 旧」）里允许出现。
        实测抓到过 ADR-0004:77 写「不设 `TIANRACER_TAP_DIR`」——
        用户读 ADR 会设错变量，而 SKIP 文案与 README 都已经改了。
        """
        root = self._repo_root()
        historical = ("以前", "曾", "改成", "旧", "历史", "原", "removed", "renamed")
        offenders = []
        for path in sorted((root / "docs").rglob("*.md")):
            for line_no, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                for stale in ("TIANRACER_TAP_DIR", "tianracer_core-serial-tap"):
                    if stale in line and not any(h in line for h in historical):
                        rel = str(path.relative_to(root)).replace("\\", "/")
                        offenders.append(f"{rel}:{line_no} 把 {stale} 写成现行用法")
        assert not offenders, "；".join(offenders)
