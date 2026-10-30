#!/usr/bin/env python3
"""Tianracer T110（RDK）整车自动测试 — 入口。

用法：
    python run.py                 交互式问答
    python run.py --host 1.128    预先指定地址

安全提示：选择运动测试后，程序会真的驱动车辆。
架空段需要轮子离地，落地段需要开阔平地且周围无人。
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

# paramiko 依赖的 cryptography 会自己报 DeprecationWarning，
# 用的是 cryptography 内部旧算法（TripleDES / Blowfish），与本项目无关，
# 但会刷满终端，对最终用户毫无意义。在 import paramiko 之前屏蔽掉。
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*CryptographyDeprecationWarning.*")
warnings.filterwarnings("ignore", message=".*TripleDES.*")
warnings.filterwarnings("ignore", message=".*Blowfish.*")

# 允许直接 python run.py 运行（把项目根加入模块搜索路径）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.paths import ensure_utf8_env, records_root  # noqa: E402
from core.reporting import render  # noqa: E402
from core.runner import Runner  # noqa: E402

DEFAULT_HOST = "192.168.1.128"
DEFAULT_USER = "sunrise"
DEFAULT_PASSWORD = "sunrise"

BANNER = r"""
==============================================================
  Tianracer T110（RDK）整车自动测试
==============================================================
  检测项 19 项，报告写入 test records/ 目录
  运动测试默认关闭；如启用，车辆会真实移动
==============================================================
"""


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        raise SystemExit(1)
    except UnicodeDecodeError:
        # Windows 管道输入时 stdin 可能不是 UTF-8，导致中文变 ?。
        # 此时退回默认值，并在后续报告里标明。
        print("  （输入编码异常，使用默认值）")
        return default
    return answer or default


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    mark = "Y/n" if default else "y/N"
    while True:
        answer = ask(f"{prompt} ({mark})", "").lower()
        if not answer:
            return default
        if answer in ("y", "yes", "是"):
            return True
        if answer in ("n", "no", "否"):
            return False
        print("  请输入 y 或 n")


def ask_choice(prompt: str, options, default: str) -> str:
    print(f"\n{prompt}")
    for key, label in options:
        marker = "（默认）" if key == default else ""
        print(f"  {key}) {label}{marker}")
    while True:
        answer = ask("选择", default).strip().lower()
        if any(answer == key for key, _ in options):
            return answer
        print("  无效选项，请重新输入")


def main() -> int:
    ensure_utf8_env()
    # 注意：**不要**把 stdin 强制改成 UTF-8。
    # 父进程（PowerShell / 管道）按系统默认编码（简体中文 Windows 是 GBK）写入，
    # 这边按 UTF-8 解读就会读出乱字符，连单个 `n` 都无法识别
    # （实测：交互问答陷入「请输入 y 或 n」死循环）。
    # stdin 保持系统默认即可；输出侧由 ensure_utf8_env() 处理。

    # 先解析参数：--help 应该只输出帮助，不先刷 banner
    parser = argparse.ArgumentParser(
        description="Tianracer T110（RDK）整车自动测试",
    )
    parser.add_argument("--host", default="", help="车辆 IP")
    parser.add_argument("--user", default="", help="SSH 用户名")
    parser.add_argument("--password", default="", help="SSH 密码")
    parser.add_argument("--mode", choices=["auto", "attach"], default="",
                        help="auto=程序自己启停 ROS；attach=车已启动")
    parser.add_argument("--no-motion", action="store_true",
                        help="强制不测运动（含微脉冲）")
    parser.add_argument("--force-motion", action="store_true",
                        help="即使电压未知/过低也执行运动测试（危险，会写进报告）")
    args = parser.parse_args()

    print(BANNER)

    host = args.host or ask("车辆 IP 地址", DEFAULT_HOST)
    user = args.user or ask("SSH 用户名", DEFAULT_USER)
    password = args.password or ask("SSH 密码", DEFAULT_PASSWORD)

    if args.mode:
        # 命令行直接给了值，不要再过一遍「a/b 选项」转换——
        # 之前这里写成 "auto" if mode == "a" else "attach"，
        # 把命令行传的 auto 也降级成了 attach（实车测试抓到的 bug）。
        mode = args.mode
    else:
        choice = ask_choice(
            "ROS 启动方式",
            [("a", "程序自动启动并关闭 bringup（推荐）"),
             ("b", "车已经跑着，只做检查")],
            "a",
        )
        mode = "auto" if choice == "a" else "attach"
    print(f"\nROS 启动方式：{'程序自动启停 bringup' if mode == 'auto' else '检测已运行的 ROS 环境'}")

    print("\n" + "-" * 62)
    print("硬件状态确认")
    print("-" * 62)

    answers: dict = {}

    # 电调状态必须先问：它是运动测试能否成立的前提。
    # 默认值不能写 True——这是事实性提问，应该让人主动确认而非默认「已开」。
    esc_on = ask_yes_no("电调（ESC）电源开关当前是打开的吗？", default=False)
    answers["电调（ESC）是否已开启"] = "是" if esc_on else "否"

    state = ask_choice(
        "车辆当前状态",
        [("a", "架空（轮子离地）"), ("b", "落地，周围空旷")],
        "a",
    )
    answers["车辆状态"] = "架空" if state == "a" else "落地空旷"

    if not esc_on:
        print("\n⚠ 电调未开启。此时车辆无法运动，运动测试将只用于验证这个事实。")

    # ---- 第一道：允许微动（微脉冲，约 1.5 cm）----
    print("\n" + "-" * 62)
    print("运动测试安全确认")
    print("-" * 62)

    motion_armed = False
    motion_level = "none"

    if args.no_motion:
        print("已通过命令行强制跳过全部运动测试。")
    else:
        print("程序需要向底盘发一次极短的指令（0.3 秒 × 0.05 m/s，约 1.5 cm），")
        print("验证电机是否真的能转。这一步能发现「通信正常但电调没开」。")
        # 默认 False：D4 定的是「运动项默认关闭」，
        # 不能出现「连按回车就让车动」的路径（Round 2 审查发现）。
        if ask_yes_no("允许车辆微动 2 cm 以内做响应检测？", default=False):
            print()
            print("=" * 62)
            print("  ⚠  请确认：")
            print("     1. 车辆周围 1 米内无人、无障碍物")
            print("     2. 遥控器在你手上，可随时接管")
            print("=" * 62)
            if ask_yes_no("以上条件已满足，可以开始？", default=False):
                motion_armed = True
            else:
                print("未确认安全条件，跳过全部运动测试。")

        # ---- 第二道：完整运动测试 ----
        if motion_armed:
            print()
            if ask_yes_no("除微脉冲外，是否还要做完整的运动测试？", default=False):
                level = ask_choice(
                    "选择测试范围",
                    [("a", "仅架空段：前进/后退/左右转向，验证响应与对称性"),
                     ("b", "架空段 + 落地段：再加真实行驶距离与转向弧度")],
                    "a",
                )
                if level == "b" and answers["车辆状态"] != "落地空旷":
                    print("\n  注意：当前标记为「架空」，落地段开始前程序会暂停，")
                    print("        提醒你把车放到开阔平地。")
                motion_level = "bench" if level == "a" else "full"

    # 硬件状态自述，写进报告
    if motion_armed:
        answers["motion_armed"] = "yes"
    if args.force_motion:
        answers["force_motion"] = "yes"

    print("\n人工检查项（会写进报告，可留空）：")
    answers["开关机"] = ask("  长按电源 3 秒能否正常启动到桌面？", "未测")
    answers["电调模式"] = ask("  电调是否为 Li 电模式 + F/R 模式？", "未测")
    answers["遥控手感"] = ask("  遥控油门是否线性、松杆能否停稳？", "未测")

    # 用一个可变的引用让闭包能看到后面才创建的 runner
    runner_ref: list = [None]

    def pause_for_ground() -> bool:
        """架空段完成后，问用户是否把车放下继续。

        等待期间刷新锁心跳：用户可能要想几分钟，
        若超过 InstanceLock 的陈旧阈值（600 秒），另一个实例就能抢锁。
        """
        if runner_ref[0] is not None and runner_ref[0]._lock is not None:
            runner_ref[0]._lock.heartbeat()
        print()
        print("=" * 62)
        print("  架空段已通过。如需测落地段：")
        print("  1. 把车放到开阔平地，轮子着地")
        print("  2. 确认周围 1 米内无人、无障碍")
        print("  3. 遥控器在手")
        print("=" * 62)
        answer = ask_yes_no("现在执行落地段吗？", default=False)
        if runner_ref[0] is not None and runner_ref[0]._lock is not None:
            runner_ref[0]._lock.heartbeat()
        return answer

    print("\n开始检测，请勿关闭窗口…\n")
    runner = Runner(
        host=host, user=user, password=password,
        answers=answers, mode=mode, motion=motion_level,
        pause_for_ground=pause_for_ground if motion_level == "full" else None,
    )
    runner_ref[0] = runner
    report = runner.run()

    # 渲染报告
    try:
        md_path, html_path = render(report, runner.record_dir)
    except Exception as exc:
        print(f"\n❌ 报告渲染失败：{exc}")
        return 2

    print("\n" + "=" * 62)
    print(f"  判定：{report.verdict}")
    print("=" * 62)
    print(f"  Markdown : {md_path}")
    print(f"  HTML     : {html_path}")
    print(f"  原始记录 : {runner.record_dir / 'raw.log'}")
    print("=" * 62)

    # 退出码供脚本化调用：
    #   0 = 全通过
    #   1 = 有 FAIL
    #   2 = 中途终止 / 未验证比例超限

    if report.failed:
        print("\n失败项：")
        for item in report.failed:
            print(f"  ❌ {item.number}. {item.title} — {item.summary}")
    if report.skipped:
        print("\n未验证项：")
        for item in report.skipped:
            print(f"  ⏭️  {item.number}. {item.title} — {item.summary}")
    if report.warned:
        print("\n告警项：")
        for item in report.warned:
            print(f"  ⚠️  {item.number}. {item.title} — {item.summary}")

    if report.failed:
        return 1
    if report.aborted_reason or report.skip_ratio > 0.2:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
