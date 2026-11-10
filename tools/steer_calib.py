#!/usr/bin/env python3
"""转向标定：反推「指令舵机角」与「真实转角」的差距。

背景
----
底盘 MCU 不回传舵机实际角度（协议只有 ODOM/IMU/Voltage/DEBUG 四类下行帧），
所以只能间接测：给定恒定转角与速度，让车走稳态圆弧，用 IMU 测车体真实
角速度 ω，再由 `R = v / ω`、`δ = atan(L / R)` 反推。

三路独立算法交叉验证
--------------------
    A 速度法    R = v_cmd / ω            依赖轮速，打滑时偏大 → δ 偏小
    B 打滑修正  R = v_real / ω            v_real = v_cmd/(1+s)
    C 弦长法    R = c / (2·sin(θ/2))      只用位移与角度

A 与 C 都会因打滑偏大；B 修正后应与之拉开。若三者接近 → 结果可信；
若 A/C 明显大于 B → 打滑主导，δ_B 更接近真值。

用法
----
    python tools/steer_calib.py --host 192.168.1.82 --speed 0.12 --seconds 15

    # 只跑指定角度
    python tools/steer_calib.py --angles 0.20 0.30 --seconds 20

    # 复用已在跑的 bringup（不自己起停）
    python tools/steer_calib.py --no-launch

安全
----
- 车必须落地、场地空旷；脚本不做架空检查，这由操作者确认
- 每轮固定时长，到点发零速
- 任一角度失败即停止后续（不带着异常状态继续跑）
- 结束后停 bringup、清残留

输出
----
终端表格 + `steer_calib.json`（原始数据，供后续复核）。
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.geometry import (correct_for_slip, slip_ratio)  # noqa: E402
from core.ssh import Remote, shquote  # noqa: E402

PROBE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "probes", "steer_calib.py")
REMOTE_PROBE = "/tmp/_tt_steer_calib.py"

PROBE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "probes", "steer_calib.py")
SPACE_PROBE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "probes", "space_probe.py")
REMOTE_PROBE = "/tmp/_tt_steer_calib.py"
REMOTE_SPACE = "/tmp/_tt_space_probe.py"

# 默认从保守的小角度开始？不 —— 反过来。
# 角度越小半径越大（R = L/tan δ），占地越多。
# 2026-11-10 按升序跑，第一个 0.10 rad 就要 5 米见方，撞了车。
# 现在按降序：先跑占地最小的，且每一组跑前都用雷达验一次。
DEFAULT_ANGLES = [0.35, 0.30, 0.25, 0.20, 0.15]

# 环境前缀由产品配置生成，不在这里硬编码工作空间路径或车型变量 ——
# 换车型、换工作空间时只改 profile.yaml 一处。
SOURCE = ""


def build_source(product_id=None):
    """从产品配置取 shell 环境前缀。

    与正式检测走同一条路（`profile.ros_env_prefix()`），所以标定用的串口、
    波特率、ROS 环境与实车检测完全一致，不会出现「标定能跑、检测跑不了」。
    """
    from core.profile import default_profile_id, load
    profile = load(product_id or default_profile_id())
    return profile.ros_env_prefix()


def run_probe(remote, args, timeout, source=None, probe=PROBE,
              remote_path=REMOTE_PROBE):
    """把探针送到车上执行，解析它输出的 RESULT JSON。"""
    src = open(probe, encoding="utf-8").read()
    encoded = base64.b64encode(src.encode("utf-8")).decode()
    arg_text = " ".join(str(a) for a in args)
    cmd = (f"echo {encoded} | base64 -d > {remote_path} && "
           f"{source or SOURCE}python3 {remote_path} {arg_text} 2>&1; "
           f"rc=$?; rm -f {remote_path}; exit $rc")
    out = remote.run(cmd, timeout=timeout).stdout or ""
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("RESULT "):
            try:
                return json.loads(line[len("RESULT "):])
            except json.JSONDecodeError:
                return {"ok": False, "reason": "bad_json", "detail": line[:200]}
    return {"ok": False, "reason": "no_result", "detail": out[-300:]}


def check_space(remote, source, steer, speed, seconds, scan_topic):
    """跑前用雷达验一次：这一步会不会撞。

    这是 2026-11-10 撞车后加的强制前置。不靠人估计场地，
    直接向雷达要答案。
    """
    return run_probe(
        remote,
        [scan_topic, 8, f"{steer:.4f}", f"{speed:.4f}", f"{seconds:.1f}"],
        timeout=60, source=source,
        probe=SPACE_PROBE, remote_path=REMOTE_SPACE)


def start_bringup(remote, source):
    remote.run("pkill -9 -f 'roslaunch tianracer' 2>/dev/null; sleep 1; "
               "pkill -9 -f tianbot_core 2>/dev/null; sleep 1; "
               "pkill -9 -f rosmaster 2>/dev/null; sleep 2; true",
               timeout=40, record=False)
    remote.run(source + "nohup roscore > /tmp/calib_roscore.log 2>&1 &",
               timeout=20, record=False)
    time.sleep(6)
    remote.run(source + "nohup roslaunch tianracer_core tianracer_core.launch "
               "> /tmp/calib_bringup.log 2>&1 &", timeout=20, record=False)
    time.sleep(10)


def stop_bringup(remote):
    remote.run("pkill -9 -f 'roslaunch tianracer' 2>/dev/null; sleep 1; "
               "pkill -9 -f tianbot_core 2>/dev/null; sleep 1; "
               "pkill -9 -f rosmaster 2>/dev/null; sleep 2; true",
               timeout=40, record=False)


def analyse(row, wheelbase):
    """从一轮原始数据算出三种方法的真实转角。"""
    imu = row.get("imu_yaw_deg")
    odom = row.get("odom_yaw_deg")
    dur = row.get("duration")
    chord = row.get("chord_m")
    v_cmd = abs(row["speed_cmd"])
    d_cmd = abs(row["steer_cmd"])

    if not imu or not dur or dur <= 0:
        return None

    omega_deg = abs(imu) / dur           # °/s，车体真实角速度
    omega_rad = math.radians(omega_deg)

    slip = slip_ratio(odom, imu) if odom is not None else None
    v_real = correct_for_slip(v_cmd, slip)

    out = {
        "steer_cmd_rad": d_cmd,
        "steer_cmd_deg": math.degrees(d_cmd),
        "omega_deg_s": omega_deg,
        "slip": slip,
        "v_real": v_real,
        "duration": dur,
        "imu_deg": imu,
        "odom_deg": odom,
    }

    # A：用指令速度
    r_a = v_cmd / omega_rad if omega_rad > 1e-9 else None
    out["R_A"] = r_a
    out["steer_A_deg"] = math.degrees(math.atan(wheelbase / r_a)) if r_a else None

    # B：打滑修正后的速度
    r_b = v_real / omega_rad if omega_rad > 1e-9 else None
    out["R_B"] = r_b
    out["steer_B_deg"] = math.degrees(math.atan(wheelbase / r_b)) if r_b else None

    # C：弦长法（不用速度）
    r_c = None
    if chord and chord > 1e-6 and abs(imu) > 1e-6:
        half = math.radians(abs(imu)) / 2.0
        s = math.sin(half)
        if s > 1e-9:
            r_c = chord / (2 * s)
    out["R_C"] = r_c
    out["steer_C_deg"] = math.degrees(math.atan(wheelbase / r_c)) if r_c else None

    return out


def _dash(value, spec="+.3f"):
    return "—" if value is None else format(value, spec)


def fmt(value, spec=".2f", dash="—"):
    return dash if value is None else format(value, spec)


def main():
    ap = argparse.ArgumentParser(description="转向角标定")
    ap.add_argument("--host", default="192.168.1.82")
    ap.add_argument("--user", default="sunrise")
    ap.add_argument("--password", default="sunrise")
    ap.add_argument("--wheelbase", type=float, default=0.255,
                    help="轴距（米），默认 0.255（2026-11-10 实测）")
    ap.add_argument("--speed", type=float, default=0.12)
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--angles", type=float, nargs="*", default=None)
    ap.add_argument("--sides", nargs="*", default=["left", "right"])
    ap.add_argument("--product", default=None,
                    help="产品 id，默认取 default_profile_id()")
    ap.add_argument("--no-launch", action="store_true",
                    help="复用已在运行的 bringup")
    ap.add_argument("--no-space-check", action="store_true",
                    help="跳过雷达空间校验（危险，不建议）")
    ap.add_argument("--precheck", action="store_true",
                    help="只做雷达空间预检，不发任何运动指令")
    ap.add_argument("--scan-topic", default="/tianracer/scan")
    ap.add_argument("--out", default="steer_calib.json")
    args = ap.parse_args()

    global SOURCE
    SOURCE = build_source(args.product)

    angles = args.angles or DEFAULT_ANGLES
    remote = Remote(host=args.host, user=args.user, password=args.password)
    remote.connect()

    rows = []
    launched = False
    try:
        if not args.no_launch:
            print("起 bringup…", flush=True)
            start_bringup(remote, SOURCE)
            launched = True

        total = len(angles) * len(args.sides)
        n = 0

        # ---- 预检：只问雷达，不发运动 ----
        if args.precheck:
            print(f"{'δ(rad)':>7} {'δ(°)':>7} {'侧':>6} {'扫掠环':>15} "
                  f"{'扫过':>7} {'判定':>8} {'环内':>7} {'余量':>7}", flush=True)
            print("-" * 72)
            ok_list = []
            for angle in angles:
                for side in args.sides:
                    signed = angle if side == "left" else -angle
                    space = check_space(remote, SOURCE, signed, args.speed,
                                        args.seconds, args.scan_topic)
                    if not space.get("ok"):
                        print(f"{angle:7.2f} {math.degrees(angle):7.2f} {side:>6} "
                              f"  预检失败: {space.get('reason')}", flush=True)
                        continue
                    sw = space.get("sweep") or {}
                    ann = sw.get("annulus") or {}
                    mark = "✅" if sw.get("verdict") == "safe" else "❌"
                    print(f"{angle:7.2f} {math.degrees(angle):7.2f} {side:>6} "
                          f"{ann.get('inner_m')}~{ann.get('outer_m'):<8} "
                          f"{ann.get('sweep_deg'):>6}° {mark} {sw.get('verdict'):>7} "
                          f"{_dash(sw.get('conflict_m')):>7} {_dash(sw.get('margin_m')):>7}",
                          flush=True)
                    if sw.get("verdict") == "safe":
                        ok_list.append(f"{angle}/{side}")
            print(f"\n安全可跑：{ok_list if ok_list else '无'}")
            print("（预检不移动车，可反复跑）")
            return 0
        for angle in angles:
            for side in args.sides:
                n += 1
                signed = angle if side == "left" else -angle
                label = f"calib_{side}_{angle:.2f}"
                print(f"[{n}/{total}] {label}  steer={signed:+.3f} rad "
                      f"({math.degrees(signed):+.2f}°)  {args.seconds:.0f}s …",
                      flush=True)

                # ---- 跑前空间校验：不靠人估计，问雷达 ----
                if not args.no_space_check:
                    space = check_space(remote, SOURCE, signed, args.speed,
                                        args.seconds, args.scan_topic)
                    if not space.get("ok"):
                        print(f"    空间校验失败：{space.get('reason')} "
                              f"{space.get('detail', '')}", flush=True)
                        raise SystemExit(4)
                    sweep = space.get("sweep") or {}
                    ann = sweep.get("annulus") or {}
                    print(f"    雷达：扫掠环 {ann.get('inner_m')}~{ann.get('outer_m')} m "
                          f"{ann.get('sweep_deg')}°，判定 {sweep.get('verdict')} "
                          f"(环内障碍 {sweep.get('conflict_m')} / 余量 {sweep.get('margin_m')} m)",
                          flush=True)
                    if sweep.get("verdict") != "safe":
                        print("    ⚠ 空间不足，跳过本组（不硬跑）", flush=True)
                        rows.append({"ok": False, "reason": "insufficient_space",
                                     "side": side, "steer_cmd": signed,
                                     "space": space, "analyse": None})
                        continue
                result = run_probe(
                    remote,
                    [args.speed, signed, args.seconds, label],
                    timeout=int(args.seconds) + 60,
                    source=SOURCE,
                )
                if not result.get("ok"):
                    print(f"    失败：{result.get('reason')} {result.get('detail', '')}",
                          flush=True)
                    raise SystemExit(2)
                if result.get("aborted"):
                    print("    被中止，停止后续", flush=True)
                    raise SystemExit(3)
                result["side"] = side
                result["analyse"] = analyse(result, args.wheelbase)
                rows.append(result)
                a = result["analyse"] or {}
                print(f"    IMU {result['imu_yaw_deg']:+.1f}°  "
                      f"odom {result['odom_yaw_deg']:+.1f}°  "
                      f"ω {fmt(a.get('omega_deg_s'))}°/s  "
                      f"打滑 {fmt(None if a.get('slip') is None else a['slip']*100, '.0f')}%  "
                      f"δ_B {fmt(a.get('steer_B_deg'))}°", flush=True)
                time.sleep(2.0)
    finally:
        if launched:
            print("停 bringup…", flush=True)
            stop_bringup(remote)
        remote.close()
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"wheelbase_m": args.wheelbase,
                       "rows": rows}, fh, ensure_ascii=False, indent=2)
        print(f"原始数据已写入 {args.out}")

    if not rows:
        print("没有采到数据")
        return 1

    # ---- 汇总表 ----
    print()
    print(f"{'侧':<4} {'δ指令°':>7} {'ω°/s':>7} {'打滑%':>6} "
          f"{'δ_A°':>7} {'δ_B°':>7} {'δ_C°':>7} {'B/指令':>7}")
    print("-" * 66)
    for r in rows:
        a = r.get("analyse")
        if not a:
            if r.get("reason") == "insufficient_space":
                print(f"{r.get('side',''):<4} {math.degrees(abs(r.get('steer_cmd') or 0)):7.2f} "
                      f"{'—— 空间不足，未执行 ——':>40}")
            continue
        ratio = (a["steer_B_deg"] / a["steer_cmd_deg"]
                 if a.get("steer_B_deg") else None)
        print(f"{r['side']:<4} {a['steer_cmd_deg']:7.2f} {a['omega_deg_s']:7.2f} "
              f"{fmt(None if a.get('slip') is None else a['slip']*100, '.0f'):>6} "
              f"{fmt(a.get('steer_A_deg')):>7} {fmt(a.get('steer_B_deg')):>7} "
              f"{fmt(a.get('steer_C_deg')):>7} {fmt(ratio, '.3f'):>7}")

    # ---- 线性度与对称性 ----
    usable = [r["analyse"] for r in rows
              if r.get("analyse") and r["analyse"].get("steer_B_deg")]
    if usable:
        ratios = [u["steer_B_deg"] / u["steer_cmd_deg"] for u in usable]
        print()
        print(f"δ_B/δ指令 比例：最小 {min(ratios):.3f}，最大 {max(ratios):.3f}，"
              f"均值 {sum(ratios)/len(ratios):.3f}")
        spread = max(ratios) - min(ratios)
        print(f"  离散度 {spread:.3f} —— "
              f"{'线性良好' if spread < 0.10 else '非线性，需查饱和或死区'}")

        for side in ("left", "right"):
            vals = [u["steer_B_deg"] for r, u in zip(rows, [r2["analyse"] for r2 in rows])
                    if r["side"] == side and u and u.get("steer_B_deg")]
            if vals:
                print(f"  {side:<5} δ_B 范围 {min(vals):.2f}° ~ {max(vals):.2f}°")

        slips = [u["slip"] for u in usable if u.get("slip") is not None]
        if slips:
            print(f"  打滑率：最小 {min(slips)*100:.0f}%，最大 {max(slips)*100:.0f}%，"
                  f"均值 {sum(slips)/len(slips)*100:.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
