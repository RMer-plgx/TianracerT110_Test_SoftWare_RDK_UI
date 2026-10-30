"""主流程编排。

这一层负责的是「顺序、依赖、安全、清理」，具体测量放在 checks/ 里。

三条硬性保证：
1. 无论怎么退出（正常 / 异常 / Ctrl-C），一定要停掉本程序启动的 roslaunch。
2. 电压低于阈值时，不再执行任何会让车动起来的检测。
3. 并发运行会互相 pkill，必须用锁文件拦住。
"""

from __future__ import annotations

import json
import os
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import remote as ros_remote
from .paths import lock_file_path, make_record_dir, resource_path
from .ssh import Remote, SSHError
from .verdict import CheckResult, Report, Status


# ---------------------------------------------------------------------------
# 并发锁
# ---------------------------------------------------------------------------

class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    """防止两个窗口同时操作同一台车。

    没有这层保护，两个实例会互相 pkill roslaunch，
    表现为「明明启动了却立刻消失」，极难排查。

    陈旧判定用**心跳时间**而不是探测进程是否存在。
    原因：Windows 上 `os.kill(pid, 0)` 不是标准意义上的「探活」，
    CPython 会把它当 CTRL_C_EVENT 走 GenerateConsoleCtrlEvent，
    对非控制台进程组会失败抛 OSError → 误判为已死，锁形同虚设（审查发现）。
    """

    def __init__(self, path: Optional[Path] = None, stale_seconds: int = 600):
        self.path = path or lock_file_path()
        self.stale_seconds = stale_seconds
        self.acquired = False

    def __enter__(self) -> "InstanceLock":
        if self.path.exists():
            try:
                info = json.loads(self.path.read_text(encoding="utf-8"))
                age = time.time() - float(info.get("updated", 0))
                pid = info.get("pid", "?")
            except (ValueError, OSError, json.JSONDecodeError):
                age, pid = self.stale_seconds + 1, "?"
            if age < self.stale_seconds:
                raise AlreadyRunning(
                    f"另一个测试进程正在运行（PID {pid}，"
                    f"{age:.0f} 秒前还有活动）。\n"
                    f"如果确认没有，删除 {self.path} 后重试。"
                )
        self._write_heartbeat()
        self.acquired = True
        return self

    def _write_heartbeat(self) -> None:
        self.path.write_text(
            json.dumps({"pid": os.getpid(), "updated": time.time()}),
            encoding="utf-8",
        )

    def heartbeat(self) -> None:
        """每个检测项开始时调用，刷新活动时间。"""
        if not self.acquired:
            return
        try:
            self._write_heartbeat()
        except OSError:
            pass

    def __exit__(self, *_exc) -> None:
        if self.acquired:
            try:
                self.path.unlink()
            except OSError:
                pass


def _pid_alive(pid: int) -> bool:
    """**未被使用**。仅作启发式参考保留，不参与任何判定。

    为什么不删：历史版本用它做并发锁的陈旧判断；在 Windows 上
    `os.kill(pid, 0)` 不是探活（会走 GenerateConsoleCtrlEvent），
    所以改用 `InstanceLock` 的心跳时间。此函数已无调用点，
    保留只为让看过旧版本的人能对上号。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        return True  # 不对 Windows 进程做任何探测，交给心跳超时判断
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


# ---------------------------------------------------------------------------
# 检测上下文
# ---------------------------------------------------------------------------

@dataclass
class Context:
    """传给每个 check 的东西。"""

    remote: Remote
    baseline: Dict
    answers: Dict[str, str] = field(default_factory=dict)
    # bringup 是否由本程序启动；False 表示用户已经自己起好了
    owns_bringup: bool = True
    # 电压低于阈值时置位，后续运动类检测必须检查它
    voltage_stop: bool = False
    voltage_v: Optional[float] = None
    # 运动测试等级：none / bench（仅架空）/ full（架空+落地）
    motion_level: str = "none"
    # 用户是否已允许车辆微动（未确认则一项都不发）
    motion_armed: bool = False
    # 用户是否显式选择忽略电压未知/过低继续运动（会写进报告）
    force_motion: bool = False
    # 电压是否确实取到了（未知不能当成安全）
    voltage_known: bool = False
    # 架空段结束后调用，返回值决定是否继续落地段。None 表示不询问。
    pause_for_ground: Optional[Callable[[], bool]] = None
    log: List[str] = field(default_factory=list)

    def say(self, text: str) -> None:
        """进度输出。同时进内存日志，便于事后查。"""
        print(text, flush=True)
        self.log.append(text)


# ---------------------------------------------------------------------------
# 检测项注册表
# ---------------------------------------------------------------------------
# 每项： (编号, 标题, 模块名, 函数名, 是否致命, 阶段)
# 阶段取值：static（不需要 bringup） / runtime（需 bringup） / motion
#
# 执行顺序按依赖排，不按编号排；报告仍按编号展示。

REGISTRY = [
    (1,  "网络连通性",       "network",  "run", True,  "static"),
    (2,  "系统身份",         "identity", "run", False, "static"),
    (3,  "RustDesk 远程桌面", "rustdesk", "run", False, "static"),
    (4,  "环境变量",         "envvars",  "run", False, "static"),
    (5,  "设备节点",         "devices",  "run", False, "static"),
    (6,  "主控硬件 ID",      "hardware", "run", False, "static"),
    (7,  "源码一致性",       "source",   "run", False, "static"),
    (10, "雷达以太网",       "lannet",   "run", False, "static"),
    (16, "teb 导航插件",     "teb",      "run", False, "static"),
    (17, "系统健康",         "health",   "run", False, "static"),
    (19, "使用痕迹",         "traces",   "run", False, "static"),
    # --- 以下需要 bringup ---
    (8,  "IMU",              "imu",      "run", True,  "runtime"),
    (9,  "激光雷达",         "lidar",    "run", True,  "runtime"),
    (11, "USB 摄像头",       "camera",   "run", False, "runtime"),
    (12, "里程计",           "odom",     "run", True,  "runtime"),
    (13, "TF 树",            "tf",       "run", False, "runtime"),
    (14, "电池电压",         "voltage",  "run", False, "runtime"),
    (15, "激光建图链路",     "mapping",  "run", False, "runtime"),
    # --- 运动 ---
    (18, "运动控制",         "motion",   "run", False, "motion"),
]


class Runner:
    def __init__(self, host: str, user: str, password: str,
                 answers: Optional[Dict[str, str]] = None,
                 mode: str = "auto", motion: str = "none",
                 pause_for_ground=None):
        """
        mode:   auto = 程序自己起停 bringup（默认）
                attach = 车已经跑着，只做检查
        motion: none   = 完全不做运动测试
                bench  = 只做架空段
                full   = 架空段 + 落地段
        """
        self.host = host
        self.user = user
        self._password = password
        self.answers = answers or {}
        self.mode = mode
        self.motion = motion
        self.pause_for_ground = pause_for_ground
        self.report = Report(
            host=host, user=user,
            started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.report.answers = dict(self.answers)
        self.record_dir: Optional[Path] = None
        self._remote: Optional[Remote] = None
        self._ctx: Optional[Context] = None
        self._lock: Optional[InstanceLock] = None

    # -- 主流程 -----------------------------------------------------------

    def run(self) -> Report:
        self.record_dir = make_record_dir(self.host)
        try:
            with InstanceLock() as lock:
                self._lock = lock
                self._run_inner()
        except AlreadyRunning as exc:
            self.report.aborted_reason = str(exc)
            print(f"\n❌ {exc}", flush=True)
        except SystemExit:
            # 用户 Ctrl-C 等场景：照样要留下记录
            self.report.aborted_reason = self.report.aborted_reason or "用户中断"
            raise
        except BaseException as exc:
            # 兵库捕获：任何未预期异常都不能让程序“默默消失”
            # 之前只捕 AlreadyRunning，其他异常会让 finally 执行完后继续向上抛，
            # 而 run.py 没有对应 try，结果就是目录建了但里面空无一物。
            detail = str(exc) or repr(exc)
            self.report.aborted_reason = (
                self.report.aborted_reason
                or f"程序异常：{type(exc).__name__} — {detail}"
            )
            print(f"\n❌ {self.report.aborted_reason}", flush=True)
            traceback.print_exc()
        finally:
            self._lock = None
            # _finalize 自己也可能出错（磁盘满、权限、编码），
            # 不能让它把主流程的异常盖掉，也不能让报告静默丢失。
            try:
                self._finalize()
            except Exception as exc:
                print(f"\n⚠ 写原始记录失败：{type(exc).__name__}: {exc}", flush=True)
        return self.report

    def _run_inner(self) -> None:
        try:
            baseline = _load_baseline()
        except Exception as exc:
            # 配置读不到时也要给出可读错误并生成报告，
            # 不能只抛一个 ModuleNotFoundError 就没了（审查发现）
            self.report.add(CheckResult(
                number=1, title="加载配置", status=Status.FAIL,
                summary=f"无法读取 config/baseline.yaml：{exc}", fatal=True,
            ))
            self.report.aborted_reason = "配置加载失败"
            print(f"\n❌ 配置加载失败：{exc}", flush=True)
            return

        try:
            self._remote = Remote(self.host, self.user, self._password)
            self._remote.connect()
        except SSHError as exc:
            self.report.add(CheckResult(
                number=1, title="网络连通性", status=Status.FAIL,
                summary=str(exc), fatal=True,
            ))
            self.report.aborted_reason = "无法建立 SSH 连接"
            return

        ctx = Context(remote=self._remote, baseline=baseline,
                      answers=self.answers, owns_bringup=(self.mode == "auto"),
                      motion_level=self.motion,
                      motion_armed=bool(self.answers.get("motion_armed")),
                      force_motion=bool(self.answers.get("force_motion")),
                      pause_for_ground=self.pause_for_ground)
        self._ctx = ctx

        try:
            if self.mode == "auto":
                ctx.say("清理残留 ROS 进程…")
                ros_remote.kill_all(self._remote, wait=3)

            # 无条件清掉上次运行残留的电压文件（不只 auto）。
            # 否则本次可能读到旧值，导致低电压车被当成合格并放行运动测试
            # （Round 3 审查发现）。
            ctx.remote.run(f"rm -f {ros_remote.VOLTAGE_TAP_PATH}", record=False)

            self._run_static_phase(ctx)
            if self._should_abort():
                return
            self._run_runtime_phase(ctx)
            if self._should_abort():
                return
            self._run_motion_phase(ctx)
        except KeyboardInterrupt:
            self.report.aborted_reason = "用户中断（Ctrl-C）"
            ctx.say("\n⚠ 收到中断信号，正在清理…")
        except Exception as exc:  # 兜底：任何未预期异常都要留痕
            # 有些异常（如 paramiko.PipeTimeout）的 str() 是空串，
            # 只写 exc 会让报告出现「程序异常：」后面什么都没有。
            detail = str(exc) or repr(exc)
            name = type(exc).__name__
            self.report.aborted_reason = f"程序异常：{name} — {detail}"
            ctx.say(f"\n❌ 程序异常：{name} — {detail}")
            traceback.print_exc()
        finally:
            if self.mode == "auto":
                ctx.say("停止 roslaunch…")
                try:
                    ros_remote.stop_launch(self._remote)
                except Exception as exc:
                    ctx.say(f"⚠ 停止 roslaunch 时出错（可能需要手动清理）：{exc}")

            # 收尾清 tap 文件。必须放在 close() **之前**——
            # _finalize() 由 run() 的 finally 在本函数返回后才调用，
            # 那时连接已关闭，里面的 run() 会直接 raise 并被吞掉，
            # 等于写了个永不执行的清理（Round 4 审查发现）。
            try:
                self._remote.run(
                    f"rm -f {ros_remote.VOLTAGE_TAP_PATH}", record=False
                )
            except Exception:
                pass

            self._remote.close()

    def _should_abort(self) -> bool:
        """致命项失败后不再继续。"""
        for result in self.report.results:
            if result.status is Status.FAIL and result.fatal:
                self.report.aborted_reason = f"致命项失败：{result.title}"
                return True
        return False

    # -- 三个阶段 ---------------------------------------------------------

    def _run_static_phase(self, ctx: Context) -> None:
        ctx.say("\n【阶段 1/3】静态检查（不需要启动 ROS）")
        for entry in REGISTRY:
            if entry[5] != "static":
                continue
            self._execute(ctx, entry)

    def _run_runtime_phase(self, ctx: Context) -> None:
        entries = [e for e in REGISTRY if e[5] == "runtime"]
        if not entries:
            return

        if self.mode == "auto":
            ctx.say("\n【阶段 2/3】启动整车 bringup")
            ros_remote.start_launch(
                ctx.remote, "tianracer_bringup", "tianracer_bringup.launch"
            )
            # 先等 rosmaster 真的监听 11311，再抢订阅电压。
            # 直接 tap 会立即报 Unable to communicate 并退出，tap 白做（Round 3）。
            if ros_remote.wait_for_master(ctx.remote, timeout=40):
                ros_remote.start_voltage_tap(ctx.remote)
            else:
                ctx.say("    ⚠ rosmaster 未在预期时间内就绪，跳过电压抢订阅")
            status = ros_remote.wait_for_nodes(ctx.remote, timeout=60)
            if not status["ready"]:
                missing = "、".join(status["missing"])  # type: ignore[arg-type]
                result = CheckResult(
                    number=8, title="IMU", status=Status.FAIL, fatal=True,
                    summary=f"bringup 未能就绪，缺少节点：{missing}",
                )
                result.add("已出现的节点", len(status["nodes"]))  # type: ignore[arg-type]
                result.add("等待时长", status["waited_sec"], "s")
                self.report.add(result)
                self.report.aborted_reason = "bringup 启动失败"
                ctx.say(f"❌ bringup 未就绪，缺少：{missing}")
                return
            ctx.say(f"✅ bringup 就绪（{len(status['nodes'])} 个节点，"  # type: ignore[arg-type]
                    f"等待 {status['waited_sec']}s）")
            # 必需节点出现 ≠ 传感器已经开始发布数据。
            # complementary_filter / ekf 等节点子进程启动后还需要几百毫秒到几秒的初始化，
            # 实车验证时曾因此把 IMU 测成「无数据」。
            ctx.say("   等待传感器稳定（5s）…")
            time.sleep(5)
            nodes_now = ros_remote.list_nodes(ctx.remote)
            missing_optional = [n for n in ros_remote.OPTIONAL_NODES
                                if n not in nodes_now]
            if missing_optional:
                ctx.say(f"   ⚠ 非必需节点未启动：{'、'.join(missing_optional)}")
        else:
            ctx.say("\n【阶段 2/3】检测已运行的 ROS 环境")
            # attach 模式下若 master 根本没起，每项都会等到超时（电压一项就要 90 秒），
            # 用户会以为程序卡死。这里快速失败并给出可操作的提示。
            if not ros_remote.is_master_running(ctx.remote):
                result = CheckResult(
                    number=8, title="IMU", status=Status.FAIL, fatal=True,
                    summary="ROS master 未运行，无法进行传感器检测",
                )
                result.detail = (
                    "你选择了「检测已运行的 ROS 环境」，但 rosnode list 失败。\n"
                    "请先在车上启动：\n"
                    "  roslaunch tianracer_bringup tianracer_bringup.launch\n\n"
                    "或改用自动模式：python run.py --mode auto"
                )
                self.report.add(result)
                self.report.aborted_reason = "ROS master 未运行"
                ctx.say("❌ ROS master 未运行，已终止（避免逐项等到超时）")
                return

        for entry in entries:
            self._execute(ctx, entry)
            if self._should_abort():
                return

    def _run_motion_phase(self, ctx: Context) -> None:
        entries = [e for e in REGISTRY if e[5] == "motion"]
        if not entries:
            return
        ctx.say("\n【阶段 3/3】运动控制")
        for entry in entries:
            self._execute(ctx, entry)

    # -- 单项执行 ---------------------------------------------------------

    def _execute(self, ctx: Context, entry) -> None:
        number, title, module_name, func_name, fatal, _phase = entry

        # 运动项即使 motion_level == "none" 也要跑：微脉冲需要验证底盘响应（D11）。
        # 只有用户连「允许微动」都没同意时才直接跳过，但要在报告里说清楚后果。
        if module_name == "motion" and not ctx.motion_armed:
            self.report.add(CheckResult(
                number=number, title=title, status=Status.SKIP, fatal=fatal,
                summary="用户未允许车辆微动，底盘响应未验证",
                detail=(
                    "串口有数据、里程计有输出，并不代表电机能转。\n"
                    "2026-10-30 实例：通信全绿，但电调未开，车完全不动。\n"
                    "若需完整验证，请重新运行并允许车辆微动。"
                ),
            ))
            ctx.say(f"  [{number:02d}] {title} — 跳过（未允许微动）")
            return

        ctx.say(f"  [{number:02d}] {title} …")
        # 刷新锁心跳，证明本进程还活着
        if getattr(self, "_lock", None) is not None:
            self._lock.heartbeat()
        started = time.time()
        try:
            module = __import__(f"checks.{module_name}", fromlist=[func_name])
            result: CheckResult = getattr(module, func_name)(ctx)
        except Exception as exc:
            result = CheckResult(
                number=number, title=title, status=Status.FAIL, fatal=fatal,
                summary=f"检测项执行异常：{exc}",
                detail=traceback.format_exc(),
            )
        result.number = number
        result.title = title
        result.fatal = fatal
        result.duration_sec = round(time.time() - started, 2)
        self.report.add(result)
        ctx.say(f"       {result.status.marker} {result.status.value.upper()}"
                f"  {result.summary}")

        # 电压联动：任何一项测出低电压，都记为全局状态
        if module_name == "voltage":
            volts = _extract_voltage(result)
            if volts is not None:
                ctx.voltage_v = volts
                ctx.voltage_known = True
                self.report.voltage_v = volts
                stop_below = ctx.baseline.get("voltage", {}).get(
                    "stop_test_below_v", 11.0)
                if volts < stop_below:
                    ctx.voltage_stop = True
                    ctx.say(f"       ⚠ 电压 {volts:.2f} V 低于 {stop_below} V，"
                            f"后续运动测试将被跳过")
            else:
                # 电压未知 → 按安全规则禁止运动（未知不能当成安全）
                ctx.voltage_known = False
                ctx.say("       ⚠ 未取得电压，后续运动测试将默认跳过")

    # -- 收尾 -------------------------------------------------------------

    def _finalize(self) -> None:
        self.report.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if self.record_dir is None:
            return
        _write_raw_log(self.record_dir, self._remote, self._ctx)


def _load_baseline() -> Dict:
    path = resource_path("config/baseline.yaml")
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "缺少 PyYAML。请先执行：pip install -r requirements.txt"
        ) from exc

    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _extract_voltage(result: CheckResult) -> Optional[float]:
    """从电压检测结果里取出数值。"""
    for item in result.measurements:
        if "电压" in item.name or "voltage" in item.name.lower():
            try:
                return float(item.value)
            except (TypeError, ValueError):
                return None
    return None


def _write_raw_log(record_dir: Path, remote: Optional[Remote],
                   ctx: Optional[Context]) -> None:
    """把每条命令的原始输入输出落盘。

    D9 要求证据可回溯：报告里写的每个数值，都要能在 raw.log 里找到出处。
    """
    lines: List[str] = []
    lines.append("# 原始命令记录")
    lines.append(f"# 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    if ctx is not None:
        lines.append("## 程序输出的进度日志")
        lines.extend(ctx.log)
        lines.append("")

    if remote is not None:
        lines.append(f"## 远端命令历史（共 {len(remote.history)} 条）")
        for index, item in enumerate(remote.history, start=1):
            lines.append("")
            lines.append(f"### [{index}] exit={item.exit_code} "
                         f"({item.duration_sec:.2f}s)")
            lines.append("```")
            lines.append("$ " + item.command)
            if item.stdout:
                lines.append("--- stdout ---")
                lines.append(item.stdout.rstrip())
            if item.stderr.strip():
                lines.append("--- stderr ---")
                lines.append(item.stderr.rstrip())
            lines.append("```")
    else:
        lines.append("## 远端命令历史：未建立连接，无记录")

    (record_dir / "raw.log").write_text("\n".join(lines), encoding="utf-8")
