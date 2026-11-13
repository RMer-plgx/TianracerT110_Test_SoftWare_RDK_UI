"""主窗口：左侧配置、中间仪表盘、右侧日志。

线程模型：
测试跑在后台 QThread 里，通过 pyqtSignal 把事件送回界面线程。
绝不从工作线程直接改控件——那是 Qt 里最常见的崩溃来源。
"""

from __future__ import annotations

import threading
import traceback
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QSize, QThread, Qt, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from core import profile as product_profile
from core import connect as conn
from core.paths import resource_path
from core.reporting import render
from core.runner import Runner, default_registry
# 模块级导入：WiredSetupWorker 与 CleanupWorker 都要用。
# 之前只有 CleanupWorker 在函数内局部 import，导致有线模式 NameError。
from core.ssh import Remote
from core.verdict import Report, Status

from .cards import CheckCard
from .switch import CapsuleSwitch, ConnectionSwitch
from .theme import palette, stylesheet


# ---------------------------------------------------------------------------
# 后台工作线程
# ---------------------------------------------------------------------------

class TestWorker(QThread):
    """在后台跑 Runner，把事件转成信号。"""

    output = pyqtSignal(str)
    check_started = pyqtSignal(int, str)
    check_done = pyqtSignal(object)
    finished_ok = pyqtSignal(object, str)   # (report, record_dir)
    failed = pyqtSignal(str, str)   # (错误信息, 堆栈)
    need_ground_confirm = pyqtSignal()

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.config = config
        self._ground_answer = None
        self._ground_event = None
        self._runner = None
        # 落地段是否因超时而放弃（区别于用户主动选 No）
        self.ground_timeout = False
        # 用户已请求停止（cancel() 置位，pause_for_ground 用它决定不弹框）
        self._cancel_flag = False

    # 供界面线程调用
    def answer_ground(self, ok: bool) -> None:
        self._ground_answer = ok
        if self._ground_event is not None:
            self._ground_event.set()

    def cancel(self) -> tuple:
        """优雅停止 + 立即停车。

        比 QThread.terminate() 安全得多——terminate 会直接杀掉线程，
        留下车上未清理的 roslaunch 进程。

        返回 (是否已请求, 零速是否发出)。
        """
        # 先唤醒可能正在等落地确认的线程，否则要等到超时
        self._cancel_flag = True
        if self._ground_event is not None:
            self._ground_event.set()

        if self._runner is not None:
            try:
                return self._runner.request_cancel()
            except Exception:
                return (True, False)
        return (True, False)

    def run(self) -> None:
        cfg = self.config

        def pause_for_ground():
            """等用户把车放到地面并确认。

            用 Event.wait() 而不是忙等 sleep 循环：
            - 不占 CPU，也不阻塞取消
            - 超时返回 None（区别于用户选 No），由调用方写进报告
            """
            import threading

            # 已经请求停止：不弹框、不阻塞，直接告诉调用方不测落地段。
            # 否则用户点了停止，界面还会弹确认框并卡 600 秒（审查指出）。
            if self._cancel_flag:
                return False

            self._ground_answer = None
            self._ground_event = threading.Event()
            self.need_ground_confirm.emit()

            # 等 10 分钟：用户要放车、确认周围安全，需要时间
            got = self._ground_event.wait(timeout=600)
            self._ground_event = None

            if self._cancel_flag and self._ground_answer is None:
                return False
            if not got or self._ground_answer is None:
                self.ground_timeout = True
                return None
            self.ground_timeout = False
            return bool(self._ground_answer)

        try:
            runner = Runner(
                host=cfg["host"],
                user=cfg["user"],
                password=cfg["password"],
                answers=cfg["answers"],
                mode=cfg["mode"],
                motion=cfg["motion"],
                profile_id=cfg.get("profile_id"),
                # sudo 密码与 SSH 密码可能不同（车上 SSH 是 sunrise，
                # sudo 是 ros）。从 config 取，让将来能在界面里配。
                sudo_password=cfg.get("sudo_password") or "ros",
                tool_name="tb-verify-ui",
                pause_for_ground=pause_for_ground if cfg["motion"] == "full" else None,
                on_output=self.output.emit,
                on_check_start=self.check_started.emit,
                on_check_done=self.check_done.emit,
            )
            self._runner = runner
            report = runner.run()

            # 渲染报告
            record_dir = str(runner.record_dir) if runner.record_dir else ""
            try:
                render(report, runner.record_dir)
            except Exception as exc:
                self.output.emit(f"⚠ 报告渲染失败：{type(exc).__name__}: {exc}")

            self.finished_ok.emit(report, record_dir)
        except Exception as exc:
            # 只捕 Exception。GUI 场景里吞掉 KeyboardInterrupt / GeneratorExit
            # 会让线程「看起来结束」但状态不一致（审查指出）。
            detail = str(exc) or repr(exc)
            self.failed.emit(f"{type(exc).__name__}: {detail}",
                             traceback.format_exc())
            traceback.print_exc()


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class UnplugCheckWorker(QThread):
    """用户点了「已拔线」之后的一次短确认。

    判据是有线地址 192.168.128.10 不再可达。
    与轮询不同：用户已经在弹窗里确认拔了线，这里只等网卡从系统消失
    （几百毫秒到几秒），不长时间无人值守探测。
    注意这不能区分「拔线」和「车断电」—— 两者都不可达，
    所以失败不报错，把判断交给用户（界面会问要不要继续）。
    """

    done = pyqtSignal(dict)

    def __init__(self, wifi_ip: str, parent=None):
        super().__init__(parent)
        self._ip = wifi_ip

    def run(self) -> None:
        try:
            unplugged = conn.verify_unplugged(grace_seconds=6.0)
            self.done.emit({"unplugged": unplugged, "ip": self._ip})
        except Exception as exc:
            self.done.emit({"error": f"{type(exc).__name__}: {exc}", "ip": self._ip})


class WiredSetupWorker(QThread):
    """有线模式的自动准备流程。

    步骤与失败处理：
      1. 确认车机 192.168.128.10 可达 —— 不可达直接报错，不动任何配置
      2. 读车上 WiFi 状态。已经连在目标网络 → 跳过重连（用户选甲）
      3. 需要重连时：扫描 → 匹配 SSID → 若需要密码则请求界面弹窗
      4. 连上后读 IP，交给界面提示拔线

    注意：第 3 步的 `nmcli device wifi connect` 会在车上**新建**
    持久化 WiFi 配置（含密码、默认自动连接）。这是让车联网的必要动作，
    不是测试副作用 —— 界面在开始前会明确告知。
    除这一步外，其余操作都不改车辆配置。
    整个流程跑在后台线程，避免连车+连WiFi 的几十秒把界面冻住。
    """

    progress = pyqtSignal(str)          # 进度文字
    need_password = pyqtSignal(str)     # 需要密码时发出 SSID
    done = pyqtSignal(dict)             # 成功：{ip, ssid, skipped}

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._config = dict(config)
        self._password = ""
        self._password_ready = threading.Event()

    def supply_password(self, value: str) -> None:
        """界面拿到用户输入的密码后调用。"""
        self._password = value or ""
        self._password_ready.set()

    def run(self) -> None:
        try:
            self._run()
        except conn.ConnectError as exc:
            self.done.emit({"error": str(exc)})
        except Exception as exc:
            self.done.emit({"error": f"{type(exc).__name__}: {exc}"})

    def _run(self) -> None:
        cfg = self._config

        self.progress.emit("检查 USB-C 有线连接…")
        if not conn.wired_link_ready(timeout=8):
            raise conn.ConnectError(
                f"没有检测到上位机连接。\n\n"
                f"请确认：\n"
                f"  · USB-C 线已插好\n"
                f"  · 车机已开机\n"
                f"  · 电脑网卡上出现了 {conn.WIRED_SUBNET_PREFIX}x 网段\n\n"
                f"（目标地址 {conn.WIRED_CAR_IP} 当前不可达）"
            )

        remote = Remote(cfg["host"], cfg["user"], cfg["password"])
        remote.connect()
        try:
            self.progress.emit("读取车辆 WiFi 状态…")
            current = conn.read_wireless(remote)

            target = conn.host_wifi()
            if not target.connected or not target.ssid:
                raise conn.ConnectError(
                    "读不到电脑当前连接的 WiFi。\n\n"
                    "请先在电脑上连好 WiFi，再重试。\n"
                    "（若电脑用的是有线网络，请改用无线模式手动填 IP）"
                )

            # 已经在同一网络：直接跳过重连
            if current.connected and conn.match_ssid(
                    target.ssid, [current.ssid]) == current.ssid:
                self.progress.emit(
                    f"车辆已连在 {current.ssid}，跳过重连")
                if not current.ip:
                    current.ip = conn.wait_for_wireless_ip(remote)
                if not current.ip:
                    raise conn.ConnectError(
                        f"车辆连着 {current.ssid} 但没拿到 IP。\n"
                        f"请在车机终端执行 ip -4 addr show wlan0 确认。"
                    )
                self.done.emit({
                    "ip": current.ip, "ssid": current.ssid, "skipped": True,
                })
                return

            self.progress.emit("扫描车辆可见的 WiFi…")
            available = conn.scan_wireless(remote)
            matched = conn.match_ssid(target.ssid, available)
            if not matched:
                shown = "、".join(available[:8]) or "（无）"
                raise conn.ConnectError(
                    f"车辆没有扫描到电脑所在的网络「{target.ssid}」。\n\n"
                    f"车辆可见：{shown}\n\n"
                    f"请确认车与电脑在同一个 WiFi 覆盖范围内。"
                )

            self.progress.emit(f"让车辆连接 {matched}…")
            password = ""
            # 先无密码试一次；需要密码时再向界面要
            ok, message = conn.join_wireless(remote, matched, "")
            if not ok and "需要密码" in message:
                self.need_password.emit(matched)
                self._password_ready.wait(timeout=180)
                if not self._password_ready.is_set():
                    raise conn.ConnectError("等待 WiFi 密码超时，流程已取消。")
                password = self._password
                self.progress.emit(f"用提供的密码连接 {matched}…")
                ok, message = conn.join_wireless(remote, matched, password)

            if not ok:
                raise conn.ConnectError(
                    f"连接「{matched}」失败：{message}\n\n"
                    f"有线连接仍然是通的，车没有被改坏。\n"
                    f"你可以重新输入密码再试。"
                )

            self.progress.emit("等待分配 IP…")
            ip = conn.wait_for_wireless_ip(remote, timeout=40)
            if not ip:
                raise conn.ConnectError(
                    f"已连上 {matched}，但 40 秒内没拿到 IP。\n"
                    f"请在车机终端执行 ip -4 addr show wlan0 查看。"
                )

            self.done.emit({"ip": ip, "ssid": matched, "skipped": False})
        finally:
            try:
                remote.close()
            except Exception:
                pass


class CodeSyncWorker(QThread):
    """对车上仓库执行 git fetch，并列出与远端的差异。

    只做 fetch —— 它只写 .git/refs/remotes，不动工作区、不影响运行中的节点。
    **不执行 pull / checkout / merge**：车上带未提交的适配改动
    （底盘类型 ackermann、波特率 115200、ackermann_msgs 依赖），
    切分支会丢掉它们，车就跑不起来了。
    """

    output = pyqtSignal(str)
    done = pyqtSignal(str, bool)     # (差异摘要, 是否全部成功)

    def __init__(self, host: str, user: str, password: str,
                 profile_id=None, parent=None):
        super().__init__(parent)
        self._host = host
        self._user = user
        self._password = password
        self._profile_id = profile_id

    def run(self) -> None:
        from core import profile as product_profile
        from core.ssh import Remote

        lines = []
        ok = True
        remote = None
        try:
            profile = product_profile.load(self._profile_id)
            repos = profile.ros.repos or []
            ws = profile.ros.workspace

            remote = Remote(self._host, self._user, self._password)
            remote.connect()

            for repo in repos:
                path = f"{ws}/src/{repo}"
                self.output.emit(f"  {repo}: git fetch …")
                res = remote.run(
                    f"cd {path} 2>/dev/null || exit 3; "
                    "timeout 40 git fetch origin 2>&1 | tail -3",
                    timeout=90, record=False,
                )
                if not res.ok:
                    ok = False
                    lines.append(f"{repo}: fetch 失败（远端不可达或无权限）")
                    continue

                info = remote.run(
                    f"cd {path} 2>/dev/null || exit 3\n"
                    'echo "BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"\n'
                    "echo \"SHORT=$(git log -1 --format='%h %s' --date=short 2>/dev/null)\"\n"
                    "echo '---'\n"
                    "git status --short 2>/dev/null | wc -l\n"
                    "git rev-list --count HEAD 2>/dev/null\n",
                    timeout=30, record=False,
                ).stdout or ""
                branch = ""
                short = ""
                dirty = 0
                for ln in info.splitlines():
                    if ln.startswith("BRANCH="):
                        branch = ln[7:].strip()
                    elif ln.startswith("SHORT="):
                        short = ln[6:].strip()
                # 未提交改动数：first line after ---
                after = info.partition("---")[2].strip().splitlines()
                if after:
                    try:
                        dirty = int(after[0].strip())
                    except ValueError:
                        dirty = 0

                lines.append(
                    f"{repo}: {branch} @ {short}"
                    + (f"，{dirty} 项未提交改动" if dirty else "，工作区干净")
                )
                self.output.emit(f"  {repo}: 已更新远端引用")

        except Exception as exc:
            ok = False
            lines.append(f"检查失败：{type(exc).__name__}: {exc}")
        finally:
            if remote is not None:
                try:
                    remote.close()
                except Exception:
                    pass

        lines.append("")
        lines.append("说明：本次只执行了 git fetch（更新 .git 里的远端引用），")
        lines.append("没有改动工作区，也没有切分支 —— 车上代码现在还是原样。")
        lines.append("")
        lines.append("如果要真正同步到上游版本，需要人工确认后单独操作：")
        lines.append("先备份车上未提交的适配改动，再切分支并重新编译。")
        self.done.emit("\n".join(lines), ok)


class CleanupWorker(QThread):
    """后台清理车上使用痕迹。

    做成线程是因为连车 + 删 83 个日志目录要几秒到几十秒，
    在界面线程里做会把窗口卡住（用户会以为程序死了）。
    """

    done = pyqtSignal(dict)

    def __init__(self, config: dict, keep_history: bool = True, parent=None):
        super().__init__(parent)
        self._config = dict(config)
        self._keep_history = keep_history

    def run(self) -> None:
        from checks import traces

        cfg = self._config
        remote = None
        try:
            remote = Remote(cfg["host"], cfg["user"], cfg["password"])
            remote.connect()

            class _Ctx:
                pass

            ctx = _Ctx()
            ctx.remote = remote
            # 默认保留命令历史：它是排查证据，不是「痕迹」
            data = traces.cleanup(
                ctx, keep_bash_history=self._keep_history
            )
        except Exception as exc:
            data = {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            if remote is not None:
                try:
                    remote.close()
                except Exception:
                    pass
        self.done.emit(data)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.theme = "dark"
        # 连接模式：wired（有线）/ wireless（无线）
        self.conn_mode = "wired"
        # 当前产品配置。加载失败时为 None，界面仍要能开起来。
        self.profile, self._profile_error = product_profile.load_safe()
        # 有线模式流程里拿到的 WiFi IP，供显示与后续连接
        self._wifi_ip = ""
        self.worker: TestWorker | None = None
        self.cards: dict = {}
        self.report: Report | None = None
        self.record_dir: Path | None = None
        self._ground_dialog = None
        # 上一轮使用的连接参数。清理使用记录、重开报告都要用它，
        # 不能只留在输入框里——用户测完可能把 IP 改掉了。
        self._last_config: dict | None = None
        self._cleaner = None
        self._wired_worker = None
        self._sync_worker = None
        self._unplug_worker = None

        _title = (self.profile.name if self.profile else "产品") + " 整车测试"
        self.setWindowTitle(_title)
        icon_path = resource_path("assets/app.ico")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        # 尺寸自适应屏幕，不要写死。
        # 实测坑：1536x864 物理屏 + 125% 缩放 → 逻辑分辨率只有 1229x691，
        # 而原来写死 minSize(1080, 700)，高度直接超出可用区，
        # 窗口被挤压、右侧日志面板被裁掉。
        self._fit_to_screen()

        self._build_ui()
        self.set_conn_mode("wired", animate=False)
        self.apply_theme()
        self._log("就绪。填写车辆地址后点击「开始检测」。")

    # -- 界面搭建 ---------------------------------------------------------

    def _fit_to_screen(self) -> None:
        """按实际可用屏幕空间定初始尺寸与最小尺寸。

        布局需要约 1100x660 才不拥挤，但小笔记本（逻辑 1229x691）
        必须能放下，所以取屏幕可用区的 92%，并给一个不超屏幕的 minSize。

        用 QScreen 而不是 QDesktopWidget：
        后者在 Qt5 高 DPI 场景下经常拿到错的值（实测同一台机器上
        Python 跑得 1920x1080、exe 跑得不一样，导致窗口被压变形）。
        """
        from PyQt5.QtWidgets import QApplication

        screen = QApplication.primaryScreen()
        available = screen.availableGeometry()
        avail_w, avail_h = available.width(), available.height()

        width = min(1280, int(avail_w * 0.92))
        height = min(820, int(avail_h * 0.92))
        self.resize(width, height)

        # 最小尺寸不能超过屏幕可用区，否则窗口会被强制挤压
        self.setMinimumSize(min(1000, avail_w), min(640, avail_h))

        # 居中
        self.move(max(0, available.center().x() - width // 2),
                  max(0, available.center().y() - height // 2))

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(12)

        outer.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setSpacing(12)
        body.addWidget(self._build_side_panel())
        body.addWidget(self._build_result_panel(), stretch=3)
        body.addWidget(self._build_log_panel(), stretch=2)
        outer.addLayout(body, stretch=1)

        outer.addWidget(self._build_footer())

    def _total_checks(self) -> int:
        """本产品的检测项数。

        用 __init__ 里缓存的 self.profile，不每次重新读 YAML ——
        default_registry() 每次调用都要解析两个配置文件（约 51 ms）。
        """
        if self.profile is not None:
            return self.profile.total_checks
        return len(default_registry())

    def _registry(self) -> list:
        """本产品的检测项清单（缓存的 profile）。"""
        if self.profile is not None:
            return self.profile.registry()
        return default_registry()

    def _build_header(self) -> QWidget:
        header = QWidget()
        layout = QHBoxLayout(header)
        layout.setContentsMargins(4, 0, 4, 0)

        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        product_name = self.profile.name if self.profile else "产品测试"
        title = QLabel(f"{product_name} 整车测试")
        title.setObjectName("HeaderTitle")
        total = self.profile.total_checks if self.profile else 0
        platform = self.profile.platform if self.profile else ""
        sub = QLabel(f"{total} 项自动检测 · {platform} 平台 · 报告自动归档")
        sub.setObjectName("HeaderSub")
        title_box.addWidget(title)
        title_box.addWidget(sub)
        layout.addLayout(title_box)
        layout.addStretch(1)

        self.theme_btn = QPushButton("切换主题")
        self.theme_btn.setObjectName("Ghost")
        self.theme_btn.setFixedWidth(96)
        self.theme_btn.clicked.connect(self.toggle_theme)
        layout.addWidget(self.theme_btn)

        return header

    def _build_mode_switch(self) -> QWidget:
        """有线 / 无线 胶囊滑动开关。

        用户 2026-11-05 明确要求：胶囊外形、一明一暗、有滑动动画。
        不用两个分段按钮 —— 那样做不出「滑块滑过去」的连续感。

        放在一个横向布局里并居中，避免被侧栏拉满宽度后变形
        （胶囊的圆角半径依赖固定高度，宽度随意拉伸会显得很扁）。
        """
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(0)

        self.mode_switch = ConnectionSwitch(resource_path("assets/icons"))
        # 固定尺寸：胶囊的圆角半径 = 高度/2，被拉伸就变形
        self.mode_switch.setFixedSize(186, CapsuleSwitch.HEIGHT)
        self.mode_switch.set_theme(self.theme)
        self.mode_switch.changed.connect(self._on_switch_changed)

        layout.addStretch(1)
        layout.addWidget(self.mode_switch)
        layout.addStretch(1)
        return box

    def _on_switch_changed(self, index: int) -> None:
        """开关自身被点击时同步状态。"""
        mode = "wired" if index == 0 else "wireless"
        if mode != self.conn_mode:
            self.set_conn_mode(mode, from_switch=True)

    def set_conn_mode(self, mode: str, from_switch: bool = False,
                      animate: bool = True) -> None:
        """切换连接模式。

        from_switch=True 表示是开关自己发出的（避免回环触发动画重启）。
        animate=False 用于程序初始化，不要一启动就滑一下。
        """
        self.conn_mode = mode
        wired = mode == "wired"
        if hasattr(self, "mode_switch"):
            self.mode_switch.set_index(0 if wired else 1,
                                       animate=animate and not from_switch)
        # 有线模式隐藏 IP 框（地址固定），无线模式显示
        self.host_edit.setVisible(not wired)
        self._update_mode_hint()

    def _update_mode_hint(self) -> None:
        """模式说明文字。有线模式要说清接下来的自动流程。"""
        if not hasattr(self, "mode_hint"):
            return
        if self.conn_mode == "wired":
            self.mode_hint.setText(
                "USB-C 直连上位机。开始后软件会自动：\n"
                "  1. 确认车机 192.168.128.10 可达\n"
                "  2. 让车连上与电脑相同的 WiFi\n"
                "  3. 读取 WiFi IP 并提示你拔线"
            )
        else:
            self.mode_hint.setText(
                "车已在 WiFi 上。请填写它的 IP 地址后开始检测。"
            )

    def _build_side_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("SidePanel")
        panel.setFixedWidth(268)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(9)

        layout.addWidget(self._section("连接方式"))
        layout.addWidget(self._build_mode_switch())

        layout.addWidget(self._section("连接"))

        # 无线模式才显示 IP 输入框；有线模式自动用 192.168.128.10
        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("车辆 IP（必填）")
        layout.addWidget(self.host_edit)

        self.user_edit = QLineEdit("sunrise")
        self.user_edit.setPlaceholderText("用户名")
        layout.addWidget(self.user_edit)

        self.pass_edit = QLineEdit("sunrise")
        self.pass_edit.setEchoMode(QLineEdit.Password)
        self.pass_edit.setPlaceholderText("密码")
        layout.addWidget(self.pass_edit)

        self.mode_hint = QLabel("")
        self.mode_hint.setObjectName("HeaderSub")
        self.mode_hint.setWordWrap(True)
        layout.addWidget(self.mode_hint)

        layout.addWidget(self._section("运行方式"))

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("程序自动启停 ROS（推荐）", "auto")
        self.mode_combo.addItem("检测已运行的 ROS", "attach")
        layout.addWidget(self.mode_combo)

        layout.addWidget(self._section("运动测试"))

        self.esc_check = QCheckBox("电调电源已开启")
        self.ground_check = QCheckBox("车辆已架空（轮子离地）")
        self.allow_pulse = QCheckBox("允许微动 2 cm 做响应检测")
        self.full_motion = QCheckBox("执行完整运动测试")

        self.ground_check.setChecked(True)
        layout.addWidget(self.esc_check)
        layout.addWidget(self.ground_check)
        layout.addWidget(self.allow_pulse)
        layout.addWidget(self.full_motion)

        hint = QLabel(
            "不勾选微动则第 20 项记为未验证。\n"
            "完整运动测试的落地段会让车左转、右转各一整圈，\n"
            "需要约 4 m × 4 m 开阔平地。"
        )
        hint.setObjectName("HeaderSub")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addStretch(1)

        self.start_btn = QPushButton("开始检测")
        self.start_btn.clicked.connect(self.start_test)
        layout.addWidget(self.start_btn)

        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("Ghost")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.request_stop)
        layout.addWidget(self.stop_btn)

        return panel

    def _build_result_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("ResultPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel("检测项")
        title.setObjectName("SectionTitle")
        top.addWidget(title)
        top.addStretch(1)
        self.counter_label = QLabel(f"0 / {self._total_checks()}")
        self.counter_label.setObjectName("HeaderSub")
        top.addWidget(self.counter_label)
        layout.addLayout(top)

        self.progress = QProgressBar()
        self.progress.setMaximum(self._total_checks())
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)

        # 按编号排序展示，执行顺序与展示顺序无关
        for index, entry in enumerate(sorted(self._registry(), key=lambda e: e[0])):
            number, title_text = entry[0], entry[1]
            card = CheckCard(number, title_text, self.theme)
            self.cards[number] = card
            grid.addWidget(card, index, 0)

        grid.setRowStretch(self._total_checks(), 1)
        scroll.setWidget(inner)
        layout.addWidget(scroll, stretch=1)

        return panel

    def _build_log_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("LogPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel("运行日志")
        title.setObjectName("SectionTitle")
        top.addWidget(title)
        top.addStretch(1)
        clear_btn = QPushButton("清空")
        clear_btn.setObjectName("Ghost")
        clear_btn.setFixedWidth(64)
        clear_btn.clicked.connect(lambda: self.log_view.clear())
        top.addWidget(clear_btn)
        layout.addLayout(top)

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("LogView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        layout.addWidget(self.log_view, stretch=1)

        return panel

    def _build_footer(self) -> QWidget:
        footer = QWidget()
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(4, 0, 4, 0)

        self.verdict_label = QLabel("尚未开始")
        self.verdict_label.setStyleSheet("font-size: 15px; font-weight: 600;")
        layout.addWidget(self.verdict_label)
        layout.addStretch(1)

        self.open_btn = QPushButton("打开报告目录")
        self.open_btn.setObjectName("Ghost")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self.open_report_dir)
        layout.addWidget(self.open_btn)

        self.view_btn = QPushButton("查看报告")
        self.view_btn.setObjectName("Ghost")
        self.view_btn.setEnabled(False)
        self.view_btn.clicked.connect(self.open_report)
        layout.addWidget(self.view_btn)

        # 「人工检测项」按钮已于 2026-11-10 删除：
        # 它只是跳到 report.html 的 #manual 锚点，而「查看报告」已经能打开同一份
        # 报告，勾选与 PDF 下载都在报告页面内完成，这个入口是多余的。
        # 实测点击无反应（系统未接管 file:// 的 fragment 跳转），留着只会误导。
        # 报告里的「需要人工现场确认的项目」一节保留 —— 它是人工复核与
        # 「确认接受」告警项的唯一载体，删按钮不等于删功能。

        # 检查代码更新：只做 git fetch + 展示差异，绝不自动 checkout/pull。
        # 为什么不做自动同步：车上带未提交的适配改动（ackermann/115200），
        # 切分支会丢掉它们，车就跑不起来了。
        self.sync_btn = QPushButton("检查代码更新")
        self.sync_btn.setObjectName("Ghost")
        self.sync_btn.setToolTip(
            "对车上仓库执行 git fetch（只更新 .git，不动工作区），"
            "然后列出本地与远端的差异"
        )
        self.sync_btn.clicked.connect(self.check_code_updates)
        layout.addWidget(self.sync_btn)

        # 清理使用记录：删掉车上 ROS 日志 / bash_history / .viminfo
        self.clean_btn = QPushButton("清理使用记录")
        self.clean_btn.setObjectName("Ghost")
        self.clean_btn.setEnabled(False)
        self.clean_btn.setToolTip("删除车辆上的 ROS 日志、命令历史与 vim 记录")
        self.clean_btn.clicked.connect(self.cleanup_traces)
        layout.addWidget(self.clean_btn)

        return footer

    @staticmethod
    def _section(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("SectionTitle")
        return label

    @staticmethod
    def _valid_host(host: str) -> bool:
        """简单的 IP / 主机名校验。

        只拦住明显不对的（如输入了中文或乱串），
        真正的可达性留给 SSH 连接阶段判定，避免自己写正则误拦。
        """
        import re
        if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host):
            return all(0 <= int(p) <= 255 for p in host.split("."))
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-]*", host)) and not host.replace(".", "").isdigit()

    # -- 主题 -------------------------------------------------------------

    def toggle_theme(self) -> None:
        self.theme = "light" if self.theme == "dark" else "dark"
        self.apply_theme()

    def apply_theme(self) -> None:
        self.setStyleSheet(stylesheet(self.theme))
        # 开关是自绘的，不吃 QSS，要单独喂颜色
        if hasattr(self, "mode_switch"):
            self.mode_switch.set_theme(self.theme)
        for card in self.cards.values():
            card.set_theme(self.theme)
        c = palette(self.theme)
        self.verdict_label.setStyleSheet(
            f"font-size: 15px; font-weight: 600; color: {c['text_muted']};"
        )

    # -- 日志 -------------------------------------------------------------

    def _log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{stamp}] {text}")

    # -- 测试流程 ---------------------------------------------------------

    def start_test(self) -> None:
        if self.conn_mode == "wired":
            self._begin_wired_flow()
            return

        host = self.host_edit.text().strip()
        if not host:
            QMessageBox.warning(self, "缺少信息", "请填写车辆 IP 地址。")
            self.host_edit.setFocus()
            return
        if not self._valid_host(host):
            QMessageBox.warning(
                self, "地址格式不对",
                f"「{host}」不像一个 IP 或主机名。\n\n"
                "请输入形如 192.168.1.128 的地址。",
            )
            self.host_edit.setFocus()
            return

        self._launch_test(host)

    # -- 有线模式流程 -----------------------------------------------------

    def _begin_wired_flow(self) -> None:
        """有线模式的完整准备流程：检测 → 连 WiFi → 提示拔线 → 开始检测。"""
        if self._wired_worker is not None and self._wired_worker.isRunning():
            return
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.warning(self, "正在检测", "请先停止当前检测。")
            return

        reply = QMessageBox.question(
            self, "有线模式说明",
            f"接下来会做三件事：\n\n"
            f"  1. 确认上位机 {conn.WIRED_CAR_IP} 可达\n"
            f"  2. 让车连上与电脑相同的 WiFi\n"
            f"     （如果车上已有该网络配置则直接启用；\n"
            f"      没有则新建一个，写到车机的 NetworkManager 配置里，\n"
            f"      含密码、默认自动连接）\n"
            f"  3. 读出 WiFi 地址后提示你拔掉 USB 线\n\n"
            f"第 2 步会改动车机的网络配置 —— 这是让车联网的必要动作，\n"
            f"但和「只读检测」不同，所以先说清楚。\n\n"
            f"需要清掉时在车机执行：nmcli connection delete <SSID>\n\n"
            f"继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        cfg = {
            "host": conn.WIRED_CAR_IP,
            "user": self.user_edit.text().strip() or "sunrise",
            "password": self.pass_edit.text() or "sunrise",
            "profile_id": self.profile.id if self.profile else None,
        }
        self.start_btn.setEnabled(False)
        self._log("有线模式：开始准备…")

        self._wired_worker = WiredSetupWorker(cfg, self)
        self._wired_worker.progress.connect(self._on_wired_progress)
        self._wired_worker.need_password.connect(self._on_need_wifi_password)
        self._wired_worker.done.connect(self._on_wired_done)
        self._wired_worker.start()

    def _on_wired_progress(self, text: str) -> None:
        self._log(text)

    def _on_need_wifi_password(self, ssid: str) -> None:
        """车辆 WiFi 需要密码时弹窗。手动输入（用户已确认接受）。"""
        text, ok = QInputDialog.getText(
            self, "需要 WiFi 密码",
            f"车辆要连接「{ssid}」，请输入该 WiFi 的密码：\n\n"
            f"（密码只在本次连接中使用，不保存到任何文件）",
            QLineEdit.Password,
        )
        if self._wired_worker is not None:
            self._wired_worker.supply_password(text if ok else "")

    def _on_wired_done(self, data: dict) -> None:
        self.start_btn.setEnabled(True)

        if data.get("error"):
            self._log(f"❌ {data['error'].splitlines()[0]}")
            QMessageBox.warning(self, "有线连接失败", str(data["error"]))
            return

        ip = data.get("ip", "")
        ssid = data.get("ssid", "")
        self._wifi_ip = ip
        self._log(f"车辆 WiFi：{ssid} → {ip}")

        # 提示拔线：等用户在弹窗里确认，再做一次短确认。
        # 不后台轮询 —— 轮询会每隔一秒起一个 ping 子进程，黑窗频闪。
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("请拔掉 USB-C 线")
        box.setText(
            f"车辆已连上 WiFi「{ssid}」\n"
            f"WiFi IP：{ip}\n\n"
            f"现在请拔掉电脑与车机之间的 USB-C 线，\n"
            f"拔好后点下面的「已拔线」。"
        )
        box.setInformativeText(
            "拔线是为了验证：不依赖有线也能连上，\n"
            "否则交付后远程连不上会很难排查。"
        )
        confirm_btn = box.addButton("已拔线", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec_()
        if box.clickedButton() is not confirm_btn:
            self._log("用户取消拔线，流程结束")
            return

        self._start_unplug_check()

    def _start_unplug_check(self) -> None:
        """用户确认拔线后的一次短确认（几秒，不轮询）。"""
        self.start_btn.setEnabled(False)
        self._log(
            f"已确认拔线，正在校验"
            f"（{conn.WIRED_CAR_IP} 数秒内仍可达会继续等）"
        )
        self._unplug_worker = UnplugCheckWorker(self._wifi_ip, self)
        self._unplug_worker.done.connect(self._on_unplug_done)
        self._unplug_worker.start()

    def _on_unplug_done(self, data: dict) -> None:
        if data.get("error"):
            self.start_btn.setEnabled(True)
            self._log(f"⚠ {data['error']}")
            QMessageBox.warning(self, "拔线校验", str(data["error"]))
            return

        if not data.get("unplugged"):
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("未检测到拔线")
            box.setText(f"{conn.WIRED_CAR_IP} 仍然可达，有线可能还连着。")
            box.setInformativeText(
                "可能原因：\n"
                "  · 线还没拔（或拔的不是车机这一端）\n"
                "  · 电脑上有其它路由到 192.168.128.0/24\n\n"
                f"要改用 WiFi 地址 {data.get('ip', '')} 继续吗？"
            )
            retry_btn = box.addButton("再检测一次", QMessageBox.AcceptRole)
            keep_btn = box.addButton("仍然继续", QMessageBox.ActionRole)
            box.addButton("取消", QMessageBox.RejectRole)
            box.exec_()
            clicked = box.clickedButton()
            if clicked is retry_btn:
                self._start_unplug_check()
                return
            if clicked is not keep_btn:
                self.start_btn.setEnabled(True)
                self._log("用户取消：未检测到拔线")
                return

        self.start_btn.setEnabled(True)
        self._log(f"✅ 有线已断开，改用 WiFi 地址 {data.get('ip', '')} 开始检测")
        self._launch_test(data.get("ip") or self._wifi_ip)

    def _launch_test(self, host: str, profile_id: str | None = None) -> None:
        """真正启动检测。有线/无线流程最终都汇到这里。"""
        # 运动相关的安全确认（Q3：只在实际会让车动的节点确认）
        allow_pulse = self.allow_pulse.isChecked()
        full_motion = self.full_motion.isChecked()

        if (allow_pulse or full_motion) and not self.esc_check.isChecked():
            reply = QMessageBox.warning(
                self, "电调未开启",
                "你未勾选「电调电源已开启」。\n\n"
                "若电调确实没开，车辆不会运动，微脉冲会判失败——\n"
                "这正是该检测的用途，可以继续。\n\n"
                "要继续吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        if (allow_pulse or full_motion):
            detail = ("完整运动测试" if full_motion else "微脉冲响应检测")
            reply = QMessageBox.question(
                self, "安全确认",
                f"即将执行：{detail}\n\n"
                "请确认：\n"
                "  · 遥控器在手，可随时接管\n"
                "  · 车轮已离地（若只做微脉冲可架空）\n"
                + ("  · 落地段会让车左转、右转各一整圈，\n"
                   "    需要约 4 m × 4 m 开阔平地（含滑移余量），周围无人无障碍\n"
                   if full_motion else
                   "  · 车辆周围 1 米内无人、无障碍\n")
                + "\n确认继续？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        # 「完整运动测试」本身就包含微脉冲，两者不是并列选项。
        # 之前勾完整测试、不勾微动时，微脉冲照样会发，
        # 用户以为自己禁掉了（审查指出）。这里改成包含关系：
        # 勾完整 → 微动脉无论如何都算开启了。
        if full_motion:
            motion = "full"
            allow_pulse = True
        elif allow_pulse:
            motion = "bench"
        else:
            motion = "none"

        answers = {
            "电调（ESC）是否已开启": "是" if self.esc_check.isChecked() else "否",
            "车辆状态": "架空" if self.ground_check.isChecked() else "落地空旷",
        }
        if motion != "none":
            answers["motion_armed"] = "yes"

        config = {
            "host": host,
            "user": self.user_edit.text().strip() or "sunrise",
            "password": self.pass_edit.text() or "sunrise",
            "mode": self.mode_combo.currentData(),
            "motion": motion,
            "answers": answers,
            "profile_id": profile_id or (self.profile.id if self.profile else None),
            "conn_mode": self.conn_mode,
            # 目前没有独立输入框，沿用车辆通用默认值。
            # 将来加输入框时只需改这里 + 界面上多一个 QLineEdit。
            "sudo_password": "ros",
        }

        # 重置界面
        for card in self.cards.values():
            card.reset()
        self.progress.setValue(0)
        total = self._total_checks()
        self.counter_label.setText(f"0 / {total}")
        self.report = None
        self.view_btn.setEnabled(False)
        # 关键：判定标签必须一并重置。
        # 它只在初始化和 _on_finished 里被写，不重置的话：
        # 上一轮的「判定：具备交付条件」绿字会一直挂在屏幕上，
        # 直到本轮跑完才刷新 —— 多车连续测试时看到的全是上一台车的结论。
        self.verdict_label.setText("尚未开始")
        self.verdict_label.setStyleSheet("font-size: 15px; font-weight: 600;")
        # 新一轮：清掉上一轮的取消标志与残留确认框
        self._cancel_flag = False
        if self._ground_dialog is not None:
            try:
                self._ground_dialog.close()
            except Exception:
                pass
            self._ground_dialog = None
        self.open_btn.setEnabled(False)
        # 检测期间锁住左侧配置：IP/账号/模式/运动选项都不应该能改。
        # config 已在上面快照过，改了也不会生效，
        # 但用户会以为改了 —— 下一步点「清理使用记录」时用的还是旧值。
        self._set_config_editable(False)
        self._log(f"开始检测：{host}")

        self._last_config = dict(config)

        self.worker = TestWorker(config)
        self.worker.output.connect(self._on_output)
        self.worker.check_started.connect(self._on_check_started)
        self.worker.check_done.connect(self._on_check_done)
        self.worker.finished_ok.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.worker.need_ground_confirm.connect(self._on_need_ground)
        self.worker.start()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

    def closeEvent(self, event) -> None:
        """关闭窗口前先把工作线程停干净。

        不处理的话：窗口销毁而线程还在跑，下一次回调发往已销毁对象，
        轻则报 wrapped C/C++ object has been deleted，
        重则进程被强杀，车上的 roslaunch 不会被执行清理（审查指出）。
        """
        if self.worker is not None and self.worker.isRunning():
            reply = QMessageBox.question(
                self, "测试进行中",
                "检测还在进行。关闭窗口会停止测试并清理车上的 ROS 进程。\n\n"
                "确定关闭吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return

            self._log("正在停止并清理…")
            self.worker.cancel()
            # 给足够时间让当前项收尾、finally 里 pkill 跑完。
            # 注意：即使超时也**不能**关窗——关了之后用户看不到日志、
            # 无法再停止，而车可能还在动（审查指出）。
            if not self.worker.wait(180000):
                QMessageBox.critical(
                    self, "无法安全关闭",
                    "后台任务 180 秒内未结束，窗口暂不关闭。\n\n"
                    "车可能仍在动作，请立即用遥控器接管。\n\n"
                    "如需强行结束：先在车上执行\n"
                    "  pkill -9 -f roslaunch; pkill -9 -f rosmaster\n"
                    "再关闭本窗口。",
                )
                event.ignore()
                return
        event.accept()

    def request_stop(self) -> None:
        """请求停止。

        不只置标志：会立即发零速让车停下。
        按钮不禁用 —— 用户点完发现车还在动时要能再点一次（审查指出）。
        """
        if self.worker is None or not self.worker.isRunning():
            return
        self._log("正在停止：已发送零速，当前检测项结束后中止…")
        stop_btn_text = self.stop_btn.text()
        self.stop_btn.setText("再次发送停车")
        try:
            requested, stop_ok = self.worker.cancel()
        except Exception as exc:
            self._log(f"⚠ 停止出错：{exc}")
            requested, stop_ok = True, False

        if stop_ok:
            self._log("■ 零速已发送，等待当前检测项收尾")
        else:
            self._log("⚠ 零速发送失败，请立即用遥控器接管")
            QMessageBox.warning(
                self, "停车指令未确认",
                "无法确认零速指令已发送到车辆。\n\n"
                "请立即用遥控器接管，或等待程序完成清理。",
            )
        self.stop_btn.setText(stop_btn_text)

    def _on_output(self, text: str) -> None:
        self._log(text)

    def _on_check_started(self, number: int, title: str) -> None:
        card = self.cards.get(number)
        if card is not None:
            card.set_running()

    def _on_check_done(self, result) -> None:
        card = self.cards.get(result.number)
        if card is not None:
            card.set_result(result.status, result.summary)
        done = sum(1 for r in self.cards.values() if r.status is not None)
        self.progress.setValue(done)
        self.counter_label.setText(f"{done} / {len(self.cards)}")

    def _on_need_ground(self) -> None:
        """落地段前的非模态确认。

        不能用 QMessageBox.question（应用级模态）：
        工作线程在 Event.wait() 上等着，而模态框会挡住主窗口，
        用户去放车、确认周围环境时很不方便，且无法查看日志。
        """
        if self._ground_dialog is not None:
            return

        dialog = QMessageBox(self)
        dialog.setWindowTitle("架空段已通过")
        dialog.setIcon(QMessageBox.Information)
        # 不用 `<strong>`：Qt 的富文本子集不认这个标签，会原样显示
        # （2026-11-10 用户看到「并<strong>左转…」）。用纯文本 + 分隔符强调。
        dialog.setText(
            "架空段已通过。\n\n"
            "把车放到开阔平地、轮子着地。\n\n"
            "落地段会让车前进后退、左右转向，\n"
            "并左转、右转各转一整圈做航向互校。\n\n"
            "场地要求：约 4 m × 4 m 无人无障碍。\n"
            "（纯圆弧只需 1.8 m，但四驱同速转弯必然打滑，\n"
            "  实测转一圈会偏离起点 1.1~1.3 m，所以要多留空间。）\n\n"
            "放好后点「继续落地段」；不需要则点「跳过」。"
        )
        continue_btn = dialog.addButton("继续落地段", QMessageBox.AcceptRole)
        skip_btn = dialog.addButton("跳过", QMessageBox.RejectRole)
        dialog.setWindowModality(Qt.NonModal)
        dialog.setModal(False)

        def handle(button) -> None:
            self._ground_dialog = None
            ok = button is continue_btn
            if self.worker is not None:
                self.worker.answer_ground(ok)
            self._log("用户确认车已落地，继续落地段" if ok else "用户选择跳过落地段")

        dialog.buttonClicked.connect(handle)
        self._ground_dialog = dialog
        dialog.show()

    def _on_finished(self, report: Report, record_dir: str) -> None:
        self.report = report

        # 以报告为准回填所有卡片。
        for result in report.results:
            card = self.cards.get(result.number)
            if card is not None and card.status is None:
                card.set_result(result.status, result.summary)
        done = sum(1 for card in self.cards.values() if card.status is not None)
        # 中止时不能把进度条拉满：只跑了 10 项就该显示 10/19
        self.progress.setValue(done)
        self.counter_label.setText(f"{done} / {len(self.cards)}")

        # 颜色直接用 core 的判定，不在 UI 层重写一份。
        # 之前手写了三档（failed/warned/其它→绿），
        # 结果把「中止」显示成绿色——正是 core 早已修掉的 bug。
        c = palette(self.theme)
        color = {
            "good": c["pass"],
            "warn": c["warn"],
            "bad": c["fail"],
        }.get(report.verdict_class, c["text_muted"])
        self.verdict_label.setStyleSheet(
            f"font-size: 15px; font-weight: 600; color: {color};"
        )
        self.verdict_label.setText(f"判定：{report.verdict}")

        if record_dir:
            self.record_dir = Path(record_dir)
            self.view_btn.setEnabled(True)
            self.open_btn.setEnabled(True)
            self._log(f"报告已生成：{self.record_dir}")

        # 清理按钮只要有过一次可用的连接参数就放开——
        # 即使本轮中途失败，也该允许把车上的痕迹清掉
        self.clean_btn.setEnabled(self._last_config is not None)

        self._cleanup_after_run()
        # 不要把进度条拉满：中途中止时只跑了几项，满格会误导（审查指出）

    def _on_failed(self, message: str, trace: str = "") -> None:
        self._log(f"❌ 程序异常：{message}")

        # 把堆栈写进报告目录。
        # 原来写「已写入控制台」——但打包时用的是 --windowed，
        # 根本就没控制台，用户无处可看（审查指出）。
        where = ""
        if self.record_dir is not None:
            try:
                log_path = self.record_dir / "error.log"
                log_path.write_text(
                    f"{message}\n\n{trace}", encoding="utf-8"
                )
                where = f"\n\n详细堆栈：{log_path}"
            except Exception:
                where = ""

        # 弹框里也附上前几行，用户不翻文件就能看到原因
        preview = "\n".join(trace.splitlines()[-6:]) if trace else ""
        body = f"测试未能完成：\n\n{message}{where}"
        if preview:
            body += f"\n\n最近几行堆栈：\n{preview}"
        QMessageBox.critical(self, "程序异常", body)

        self._cleanup_after_run()

    def _set_config_editable(self, editable: bool) -> None:
        """检测期间锁定左侧配置面板。

        为什么要锁：`_launch_test` 已经把 config 快照下来了，
        检测中改界面不会影响本次运行，但用户会以为改了。
        而「清理使用记录」用的是 `_last_config`，也是旧值 ——
        用户改了 IP 再点清理，清的还是上一台车。
        """
        for widget in (self.host_edit, self.user_edit, self.pass_edit,
                       self.mode_combo, self.esc_check, self.ground_check,
                       self.allow_pulse, self.full_motion):
            widget.setEnabled(editable)
        # 连接模式开关也一并锁住
        switch = getattr(self, "mode_switch", None)
        if switch is not None:
            switch.setEnabled(editable)

    def _cleanup_after_run(self) -> None:
        """每一轮结束后的共同收尾。"""
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        # 解锁左侧配置：检测结束（成功/失败/中止）都要恢复
        self._set_config_editable(True)
        # 关掉可能还在显示的落地确认框
        if self._ground_dialog is not None:
            try:
                self._ground_dialog.close()
            except Exception:
                pass
            self._ground_dialog = None
        # 释放线程对象，避免下一轮覆盖后旧线程仍在跑时被 GC 销毁
        if self.worker is not None and not self.worker.isRunning():
            self.worker.deleteLater()
            self.worker = None

    # -- 报告 -------------------------------------------------------------

    def open_report(self) -> None:
        if self.record_dir is None:
            return
        html = self.record_dir / "report.html"
        target = html if html.exists() else self.record_dir
        import os
        os.startfile(str(target))  # noqa: S606

    def open_report_dir(self) -> None:
        if self.record_dir is None:
            return
        import os
        os.startfile(str(self.record_dir))  # noqa: S606

    # -- 清理 -------------------------------------------------------------

    def check_code_updates(self) -> None:
        """对车上仓库执行 git fetch，列出与远端的差异。

        **只读 .git，不动工作区。** 不做 checkout / pull / merge ——
        车上带着未提交的适配改动（底盘类型、波特率、依赖），
        切分支会丢掉它们，车直接跑不起来。

        完整的差异分析见 .codewhale/review/CODE_SYNC_COMPARISON.md。
        """
        host = self.host_edit.text().strip()
        if not host or not self._valid_host(host):
            QMessageBox.warning(self, "缺少信息",
                                "请先填写正确的车辆 IP。")
            return
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "正在检测",
                                    "检测进行中，请等结束或先停止。")
            return
        if self._sync_worker is not None and self._sync_worker.isRunning():
            return

        self.sync_btn.setEnabled(False)
        self._log("检查代码更新：对车上仓库执行 git fetch（不会改动工作区）…")
        self._sync_worker = CodeSyncWorker(
            host, self.user_edit.text().strip() or "sunrise",
            self.pass_edit.text() or "sunrise",
            self.profile.id if self.profile else None,
        )
        self._sync_worker.output.connect(self._on_output)
        self._sync_worker.done.connect(self._on_code_sync_done)
        self._sync_worker.start()

    def _on_code_sync_done(self, summary: str, ok: bool) -> None:
        self.sync_btn.setEnabled(True)
        self._log("代码检查完成。")
        box = QMessageBox(self)
        box.setWindowTitle("代码更新检查")
        box.setIcon(QMessageBox.Information if ok else QMessageBox.Warning)
        box.setText("已执行 git fetch（未改动工作区）")
        box.setDetailedText(summary)
        box.setStandardButtons(QMessageBox.Ok)
        box.exec_()
        if self._sync_worker is not None:
            self._sync_worker.deleteLater()
            self._sync_worker = None

    def cleanup_traces(self) -> None:
        """手动清理车上使用痕迹。"""
        cfg = self._last_config
        if not cfg:
            QMessageBox.information(
                self, "没有连接信息",
                "先完成一次检测，程序才知道要连哪台车。",
            )
            return
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.warning(
                self, "正在检测",
                "检测正在进行，请先停止，避免清理掉正在写的日志。",
            )
            return
        if self._cleaner is not None and self._cleaner.isRunning():
            return

        reply = QMessageBox.question(
            self, "确认清理",
            f"将删除 {cfg['host']} 上的以下内容：\n\n"
            "  · ~/.ros/log 下的全部 ROS 日志\n"
            "  · ~/.viminfo\n\n"
            "命令历史 ~/.bash_history 会**保留**（现场排查需要）。\n"
            "  如需一并清空，请在 config/baseline.yaml 里\n"
            "  把 traces.keep_bash_history 改为 false。\n\n"
            "删除后不可恢复。确认继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self.clean_btn.setEnabled(False)
        self._log("清理使用记录…")
        self._cleaner = CleanupWorker(cfg, keep_history=True, parent=self)
        self._cleaner.done.connect(self._on_cleanup_done)
        self._cleaner.start()

    def _on_cleanup_done(self, data: dict) -> None:
        self.clean_btn.setEnabled(True)
        if data.get("error"):
            self._log(f"❌ 清理失败：{data['error']}")
            QMessageBox.warning(self, "清理失败", str(data["error"]))
            return

        message = (
            f"ROS 日志：{data.get('logs_before_mb')} MB → "
            f"{data.get('logs_after_mb')} MB"
            f"（{data.get('logs_after_dirs')} 个条目）\n"
            f"bash_history：{data.get('hist_before')} → "
            f"{data.get('hist_after')} 行\n"
            f".viminfo：{'已删除' if data.get('vim_after') == 0 else '仍存在'}"
        )
        self._log("✅ 清理完成：" + message.replace("\n", "；"))
        QMessageBox.information(self, "清理完成", message)

