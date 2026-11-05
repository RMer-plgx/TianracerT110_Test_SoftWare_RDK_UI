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

from . import profile as product_profile
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
    # 产品配置档。话题名、工作空间、检测项清单都从这里取，
    # 代码里不再出现任何具体产品名。
    profile: "product_profile.Profile" = None
    # 用户输入的连接地址（TianbotOffice-5G 下看到的车 IP）。
    # 报告的「本机地址」用它，不再拿 hostname -I 的全部网卡凑数。
    host: str = ""
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
    # 落地段转向互校是否真的做成功（左右两圈都过）。
    # 由 checks/motion.py 写入，供 IMU / 里程计两项回填状态用。
    spin_verified: bool = False
    # 落地段是否执行过（用于区分「没做」和「做了但没通过」）
    ground_attempted: bool = False
    # 架空段结束后调用，返回值决定是否继续落地段。None 表示不询问。
    # 架空段结束后调用：True=继续落地，False=用户选 No，None=超时未回应
    pause_for_ground: Optional[Callable[[], Optional[bool]]] = None
    log: List[str] = field(default_factory=list)
    # GUI 用：进度回调。say() 每调一次就通知界面，
    # 让日志能实时滚动，而不是等整轮跑完才一次性输出。
    on_output: Optional[Callable[[str], None]] = None
    # GUI 用：单项检测开始/结束的结构化回调，
    # 比解析 say() 的文本可靠——界面靠它更新每个卡片的状态。
    on_check_start: Optional[Callable[[int, str], None]] = None
    on_check_done: Optional[Callable[[object], None]] = None
    # GUI 用：用户点击停止时置位，检测项之间检查，优雅中止
    cancel_requested: bool = False

    def say(self, text: str) -> None:
        """进度输出。同时进内存日志，便于事后查。"""
        print(text, flush=True)
        self.log.append(text)
        if self.on_output is not None:
            try:
                self.on_output(text)
            except Exception:
                # 界面回调出错不能影响测试本身
                pass


# ---------------------------------------------------------------------------
# 检测项清单
# ---------------------------------------------------------------------------
# 19 项不再写死在这里，改为由产品配置档提供：
#
#     products/<产品>/profile.yaml   →  checks.items
#
# 每项的格式仍是 (编号, 标题, 模块名, 函数名, 是否致命, 阶段)，
# 由 core.profile.CheckSpec.as_registry_tuple() 生成。
#
# **顺序即执行顺序**，profile 加载时会校验 static → runtime → motion
# 不回头的约束。


def default_registry() -> List[tuple]:
    """默认产品的检测项清单。

    仅为向后兼容保留：老代码/测试直接 `from core.runner import REGISTRY`。
    新代码应当用 `Runner.profile.registry()` —— 那才支持多产品。
    """
    return product_profile.load().registry()


class Runner:
    def __init__(self, host: str, user: str, password: str,
                 answers: Optional[Dict[str, str]] = None,
                 mode: str = "auto", motion: str = "none",
                 profile_id: Optional[str] = None,
                 tool_name: str = "tianbot_prodtest",
                 pause_for_ground=None,
                 on_output=None, on_check_start=None, on_check_done=None):
        """
        mode:   auto = 程序自己起停 bringup（默认）
                attach = 车已经跑着，只做检查
        motion: none   = 完全不做运动测试
                bench  = 只做架空段
                full   = 架空段 + 落地段
        profile_id: 产品 id（products/<id>/）。None = 用默认产品。
        """
        self.host = host
        self.user = user
        self._password = password
        self.answers = answers or {}
        self.mode = mode
        self.motion = motion
        # 产品配置档。_run_inner 里加载成功后会填进来；
        # 加载失败时保持 None，request_cancel 等路径要能容忍。
        self.profile_id = profile_id
        self.tool_name = tool_name
        self.profile: Optional["product_profile.Profile"] = None
        self.pause_for_ground = pause_for_ground
        # GUI 回调：实时日志与单项状态
        self.on_output = on_output
        self.on_check_start = on_check_start
        self.on_check_done = on_check_done
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
        # 建目录可能失败：exe 放在 C:\Program Files 或只读介质时抛 PermissionError。
        # 这一步原来在 try 之外，异常会一路冒到界面，导致一份报告都不生成（审查指出）。
        try:
            self.record_dir = make_record_dir(self.host)
        except Exception as exc:
            self.report.aborted_reason = (
                f"无法创建报告目录：{type(exc).__name__}: {exc}。"
                f"请把程序放到可写目录（如桌面）后重试。"
            )
            return self.report
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
            profile = product_profile.load(self.profile_id)
        except product_profile.ProfileError as exc:
            # 配置读不到时也要给出可读错误并生成报告，
            # 不能只抛一个 KeyError 就没了（审查发现）
            self.report.add(CheckResult(
                number=1, title="加载产品配置", status=Status.FAIL,
                summary=f"产品配置有问题：{exc}", fatal=True,
            ))
            self.report.aborted_reason = "配置加载失败"
            print(f"\n❌ 配置加载失败：{exc}", flush=True)
            return

        self.profile = profile
        baseline = profile.baseline
        # 报告头部要标明这次用的是哪套配置
        self.report.product = profile.name
        self.report.product_id = profile.id
        self.report.product_revision = profile.revision
        self.report.tool_name = self.tool_name

        try:
            self._remote = Remote(self.host, self.user, self._password)
            self._remote.connect()
        except SSHError as exc:
            result = CheckResult(
                number=1, title="网络连通性", status=Status.FAIL,
                summary=str(exc), fatal=True,
            )
            self.report.add(result)
            self.report.aborted_reason = "无法建立 SSH 连接"
            # 此时 Context 还没建，回调不可用，直接通知界面，
            # 否则第 1 项会一直显示「等待中」（实测发现）
            if self.on_check_done is not None:
                try:
                    self.on_check_done(result)
                except Exception:
                    pass
            return

        ctx = Context(remote=self._remote, baseline=baseline, profile=profile,
                      host=self.host,
                      answers=self.answers, owns_bringup=(self.mode == "auto"),
                      motion_level=self.motion,
                      motion_armed=bool(self.answers.get("motion_armed")),
                      force_motion=bool(self.answers.get("force_motion")),
                      pause_for_ground=self.pause_for_ground,
                      on_output=self.on_output,
                      on_check_start=self.on_check_start,
                      on_check_done=self.on_check_done)
        self._ctx = ctx

        try:
            if self.mode == "auto":
                ctx.say("清理残留 ROS 进程…")
                ros_remote.kill_all(self._remote, profile, wait=3)

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
            # 落地段结束后，回填「架空段测了一半」的项（IMU / 里程计）
            self._resolve_ground_dependents(ctx)
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
                    ros_remote.stop_launch(self._remote, profile)
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

            # 使用痕迹清理。必须插在这里：
            #   - roslaunch 已停——否则清掉的日志马上又被写回来
            #   - 连接还没断——close() 之后任何 run() 都会被默默吐掉
            self._cleanup_traces(ctx)

            self._remote.close()

    def _cleanup_traces(self, ctx: Context) -> None:
        """测试结束后按配置清理车上使用痕迹，并把结果写回第 19 项。

        默认 **不清理**（`traces.auto_cleanup: false`）。
        原因：清理会删 `$HOME` 下的持久文件，属于「修改车辆状态」，
        不能在用户不知情时作为测试的副作用发生。
        需要清时用界面「清理使用记录」按钮显式执行。

        即使开启 auto_cleanup，也会保留 .bash_history
        （`keep_bash_history: true`）—— 命令历史是排查证据，不是痕迹。
        """
        result = next((r for r in self.report.results if r.number == 19), None)
        traces_cfg = ctx.baseline.get("traces", {})

        if not traces_cfg.get("auto_cleanup", False):
            # 不清理时也要在报告里说清楚，避免读者以为车被清过
            if result is not None and "不会自动清理" not in result.summary:
                result.summary += "；本次未清理车上痕迹"
            return

        if self.report.aborted_reason:
            if result is not None:
                result.summary += "；本次未完整执行，保留痕迹供排查"
            return

        keep_hist = bool(traces_cfg.get("keep_bash_history", True))
        ctx.say("清理使用痕迹…")
        try:
            module = __import__("checks.traces", fromlist=["cleanup"])
            data = module.cleanup(ctx, keep_bash_history=keep_hist)
        except Exception as exc:
            data = {"error": f"{type(exc).__name__}: {exc}"}

        if data.get("error"):
            ctx.say(f"       ⚠ 痕迹清理失败：{data['error']}")
            if result is not None:
                result.status = Status.WARN
                result.summary = f"使用痕迹清理失败：{data['error']}"
            self._write_cleanup_log({"error": data["error"]})
            return

        if result is None:
            result = CheckResult(number=19, title="使用痕迹", status=Status.PASS)
            self.report.add(result)

        logs_before = data.get("logs_before_mb")
        logs_after = data.get("logs_after_mb")
        dirs_after = data.get("logs_after_dirs")
        hist_before = data.get("hist_before")
        hist_after = data.get("hist_after")
        vim_after = data.get("vim_after")

        # du -sm 对空目录也返回 1 MB（4K 块向上取整），所以体积只用于展示，
        # 是否清干净看条目数。
        result.add("清理后 ROS 日志",
                   (f"{dirs_after} 个条目 / {logs_after} MB"
                    if dirs_after is not None and logs_after is not None
                    else "读取失败"),
                   expected="0 个条目")
        result.add("清理后 bash_history",
                   hist_after if hist_after is not None else "读取失败", "行",
                   note="已保留（keep_bash_history）" if keep_hist else "已清空")
        result.add("清理后 .viminfo",
                   "不存在" if vim_after == 0 else "仍存在")

        # 清干净的判据：日志清空 + viminfo 删除 + 历史按策略处理
        clean_ok = (dirs_after == 0 and vim_after == 0
                    and (keep_hist or hist_after == 0))
        result.status = Status.PASS if clean_ok else Status.WARN
        result.summary = (
            f"测试结束后已清理：ROS 日志 {logs_before} MB → {logs_after} MB"
            f"（{dirs_after} 个条目），"
            + (f"bash_history 保留 {hist_after} 行"
               if keep_hist else
               f"bash_history {hist_before} → {hist_after} 行")
            + ("，.viminfo 已删除" if vim_after == 0 else "，.viminfo 仍在")
        )
        if not clean_ok:
            result.summary += "（部分痕迹未清干净，交付前请手工确认）"
        ctx.say(f"       {result.status.marker} {result.summary}")

        # 报告开头声明：本次删了车上的哪些东西
        removed = ["ROS 日志"]
        if vim_after == 0:
            removed.append(".viminfo")
        if not keep_hist:
            removed.append(".bash_history")
        self.report.cleanup_declaration = (
            "本次测试已清理车辆使用痕迹：" + "、".join(removed)
            + ("（命令历史已保留）" if keep_hist else "")
            + "。如需完整现场记录，请在下次测试时关闭 traces.auto_cleanup。"
        )

        self._write_cleanup_log(data)

    def _write_cleanup_log(self, data: dict) -> None:
        """在报告目录留一份 cleanup.log，方便事后追溯清理动作。"""
        if self.record_dir is None:
            return
        try:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            lines = [f"# 清理记录 {stamp}", ""]
            for key in ("error", "kept_history", "logs_before_mb", "logs_before_dirs",
                        "logs_after_mb", "logs_after_dirs", "hist_before",
                        "hist_after", "vim_after"):
                if key in data:
                    lines.append(f"{key} = {data[key]}")
            (self.record_dir / "cleanup.log").write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )
        except Exception:
            pass

    def _should_abort(self) -> bool:
        """致命项失败或用户请求停止时，不再继续。"""
        if self._ctx is not None and getattr(self._ctx, "cancel_requested", False):
            self.report.aborted_reason = "用户手动停止"
            return True
        for result in self.report.results:
            if result.status is Status.FAIL and result.fatal:
                self.report.aborted_reason = f"致命项失败：{result.title}"
                return True
        return False

    def request_cancel(self) -> tuple:
        """供外部（GUI）请求停止。

        做两件事：
        1. 置取消标志 → 检测项之间、运动段之间不再继续
        2. **立即发零速** → 车马上停，不用等当前段跑完

        返回 (cancel_requested, stop_ok) 供界面展示。
        """
        if self._ctx is None:
            return (False, False)
        self._ctx.cancel_requested = True

        # 只有在自动模式下（ROS 由本程序管理）才由这里发停车；
        # 否则用户自己管车，不该被我们插入一条指令。
        stop_ok = False
        try:
            from .ssh import Remote
            quick = Remote(self.host, self.user, self._password, timeout=6)
            quick.connect()
            try:
                # 先通知探针自己停：转圈探针 20 Hz 发指令，
                # 直接发零速会被它覆盖，车停不下来。
                quick.run("touch /tmp/tt_spin_abort", timeout=8, record=False)
                cmd_topic = (self.profile.ros.topic("cmd")
                             if self.profile else "/tianracer/ackermann_cmd")
                for _ in range(2):
                    quick.run(
                        ros_remote.ros(
                            f"timeout 3 rostopic pub -1 {cmd_topic} "
                            "ackermann_msgs/AckermannDrive "
                            "'{speed: 0.0, steering_angle: 0.0}'",
                            self.profile,
                        ),
                        timeout=12, record=False,
                    )
                stop_ok = True
            finally:
                quick.close()
        except Exception:
            stop_ok = False
        return (True, stop_ok)

    # -- 三个阶段 ---------------------------------------------------------

    def _run_static_phase(self, ctx: Context) -> None:
        ctx.say("\n【阶段 1/3】静态检查（不需要启动 ROS）")
        for entry in self.profile.registry():
            if entry[5] != "static":
                continue
            self._execute(ctx, entry)

    def _run_runtime_phase(self, ctx: Context) -> None:
        entries = [e for e in self.profile.registry() if e[5] == "runtime"]
        if not entries:
            return

        if self.mode == "auto":
            ctx.say("\n【阶段 2/3】启动整车 bringup")
            ros_remote.start_launch(ctx.remote, profile, 
                ctx.remote, "tianracer_bringup", "tianracer_bringup.launch"
            )
            # 先等 rosmaster 真的监听 11311，再抢订阅电压。
            # 直接 tap 会立即报 Unable to communicate 并退出，tap 白做（Round 3）。
            if ros_remote.wait_for_master(ctx.remote, profile, timeout=40):
                ros_remote.start_voltage_tap(ctx.remote, profile)
            else:
                ctx.say("    ⚠ rosmaster 未在预期时间内就绪，跳过电压抢订阅")
            status = ros_remote.wait_for_nodes(ctx.remote, profile, timeout=60)
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
            nodes_now = ros_remote.list_nodes(ctx.remote, profile)
            missing_optional = [n for n in profile.ros.nodes_optional
                                if n not in nodes_now]
            if missing_optional:
                ctx.say(f"   ⚠ 非必需节点未启动：{'、'.join(missing_optional)}")
        else:
            ctx.say("\n【阶段 2/3】检测已运行的 ROS 环境")
            # attach 模式下若 master 根本没起，每项都会等到超时（电压一项就要 90 秒），
            # 用户会以为程序卡死。这里快速失败并给出可操作的提示。
            if not ros_remote.is_master_running(ctx.remote, profile):
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
        entries = [e for e in self.profile.registry() if e[5] == "motion"]
        if not entries:
            return
        ctx.say("\n【阶段 3/3】运动控制")
        for entry in entries:
            self._execute(ctx, entry)

    def _resolve_ground_dependents(self, ctx: Context) -> None:
        """落地段结束后，把「待落地验证」的项落定。

        背景：IMU / 里程计的频率和静态指标在架空段就能测，但
        「航向准不准」必须落地转一整圈比对才成立。
        架空段一测完就标绿，等于把「测了一半」当成「测完了」。

        所以架空段结束时它们标 PENDING；这里根据落地互校结果落定：
          · 互校通过 → PASS
          · 没做 / 没通过 → 保持 PENDING（未验证，计入未验证比例）
        """
        dependents = [r for r in self.report.results if r.needs_ground]
        if not dependents:
            return

        verified = bool(getattr(ctx, "spin_verified", False))
        for item in dependents:
            if verified:
                item.status = Status.PASS
                item.summary = item.summary.replace(
                    "航向精度待落地互校", "航向精度已由落地转向互校确认")
                ctx.say(f"       ↑ [{item.number:02d}] {item.title} "
                        f"落地互校通过，转为 PASS")
            else:
                # 保持 PENDING：只测了一半，不能算通过
                item.summary = item.summary.replace(
                    "航向精度待落地互校",
                    "航向精度未验证（落地互校未执行或未通过）")
                ctx.say(f"       ⏳ [{item.number:02d}] {item.title} "
                        f"保持待验证（落地互校未通过）")

    # -- 单项执行 ---------------------------------------------------------

    def _execute(self, ctx: Context, entry) -> None:
        number, title, module_name, func_name, fatal, _phase = entry

        # 用户已请求停止：当前项直接跳过，不再启动。
        # 只在阶段边界检查的话，最长要等一项跑完（电压 90 秒），
        # 这段时间 GUI 上无法中止（审查指出）。
        if getattr(ctx, "cancel_requested", False):
            self.report.add(CheckResult(
                number=number, title=title, status=Status.SKIP, fatal=fatal,
                summary="用户手动停止，未执行",
            ))
            return

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
        # 通知界面：该项开始
        if ctx.on_check_start is not None:
            try:
                ctx.on_check_start(number, title)
            except Exception:
                pass
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
        # 通知界面：该项结束（带完整结果，界面可展示实测值）
        if ctx.on_check_done is not None:
            try:
                ctx.on_check_done(result)
            except Exception:
                pass

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
