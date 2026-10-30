"""ROS 环境管理：前缀、生命周期、进程清理。

这一层封装了 2026-10-30 排查中踩过的所有坑：

1. 非交互 shell 不加载 .bashrc 末尾的 ros1_setup()，必须显式 source。
2. devel/etc/catkin/profile.d/99.my_tianracer.sh 的错误默认值会覆盖
   ~/.bashrc 的正确值，导致底盘打不开（BASE_PORT=/dev/tianbot_base）。
   这里在会话内临时覆盖，不改车辆文件。
3. bringup 退出不干净会残留 rosmaster 占住 11311 端口，
   下次启动直接失败。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .ssh import CommandResult, Remote

WORKSPACE = "~/tianracer_ros1_ws"

# 这些是 2026-10-30 验证过的正确值。顺序重要：先 source 再覆盖，
# 因为 setup.bash 会在 source 过程中把错误默认值写进环境。
_ENV_SETUP = [
    "source /opt/ros/noetic/setup.bash",
    "source {ws}/devel/setup.bash".format(ws=WORKSPACE),
    "export ROS_MASTER_URI=http://localhost:11311",
    "export DISABLE_ROS1_EOL_WARNINGS=True",
    # --- 下面这些覆盖掉 env-hooks 的错误默认值 ---
    "export TIANRACER_BASE_PORT=/dev/ttyUSB0",
    "export TIANRACER_BASE_BAUDRATE=115200",
    "export TIANRACER_LIDAR=richbeam_lidar",
    "export TIANRACER_LIDAR_IP=192.168.198.2",
    "export TIANRACER_LIDAR_PORT=/dev/tianbot_lidar",
    "export TIANRACER_NAME=tianracer",
]

# 「先 source，再把 ROS_IP 设成本机地址」——多网卡机器上 rosnode/rostopic
# 需要知道对端能访问哪个地址，否则可能选错网卡。
# ROS_IP 用第一块网卡的地址，与车上 ros1_setup() 的做法保持一致。
ROS_ENV_PREFIX = (
    "source /opt/ros/noetic/setup.bash >/dev/null 2>&1; "
    f"source {WORKSPACE}/devel/setup.bash >/dev/null 2>&1; "
    "export ROS_MASTER_URI=http://localhost:11311; "
    "export ROS_IP=$(hostname -I | awk '{print $1}'); "
    "export DISABLE_ROS1_EOL_WARNINGS=True; "
    "export TIANRACER_BASE_PORT=/dev/ttyUSB0; "
    "export TIANRACER_BASE_BAUDRATE=115200; "
    "export TIANRACER_LIDAR=richbeam_lidar; "
    "export TIANRACER_LIDAR_IP=192.168.198.2; "
    "export TIANRACER_LIDAR_PORT=/dev/tianbot_lidar; "
    "export TIANRACER_NAME=tianracer; "
)


def ros(cmd: str) -> str:
    """给一条 ROS 命令加上环境前缀。"""
    return ROS_ENV_PREFIX + cmd


@dataclass
class EnvVarDiff:
    name: str
    expected: str
    actual: Optional[str]
    known_deviation_reason: str = ""

    @property
    def matches(self) -> bool:
        return self.actual == self.expected


def read_env_vars(remote: Remote) -> Dict[str, str]:
    """读取当前进程环境里的 TIAN* 变量。

    **不能用 ros() 前缀**：ROS_ENV_PREFIX 里已经 export 了
    TIANRACER_BASE_PORT / BAUDRATE / LIDAR 的正确值，
    用它去读只会读到自己的修正结果，永远检不出车辆的真实配置偏差。
    （审查发现的漏洞：第 4 项永远 PASS，“已临时修正”永不触发。）

    这里只 source，不 export，让车辆自己的值原样透出。
    """
    read_only_prefix = (
        "source /opt/ros/noetic/setup.bash >/dev/null 2>&1; "
        f"source {WORKSPACE}/devel/setup.bash >/dev/null 2>&1; "
    )
    result = remote.run(read_only_prefix + "env | grep '^TIAN' | sort")
    vars_: Dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            vars_[key.strip()] = value.strip()
    return vars_


def diff_env_vars(actual: Dict[str, str], expected: Dict[str, str],
                  known: Optional[Dict[str, dict]] = None) -> List[EnvVarDiff]:
    """逐项比对期望值与实际值。"""
    known = known or {}
    diffs: List[EnvVarDiff] = []
    for name, want in expected.items():
        got = actual.get(name)
        if got == want:
            continue
        reason = ""
        if name in known and known[name].get("actual") == got:
            reason = known[name].get("reason", "")
        diffs.append(EnvVarDiff(name=name, expected=want, actual=got,
                                known_deviation_reason=reason))
    return diffs


# ---------------------------------------------------------------------------
# roslaunch 生命周期
# ---------------------------------------------------------------------------

def kill_all(remote: Remote, wait: int = 5) -> None:
    """清掉 tianracer 相关的 roslaunch / rosmaster，确保端口释放。

    先 INT 让 roslaunch 正常收尾（它会自己 kill 子节点），再 KILL 兜底。

    注意匹配范围：
      - roslaunch / tianbot_core 只按 `tianracer` 匹配，不会误杀其他包
      - ros­master **会全杀**（它只是个名字），因为本工具要求独占 11311
      - 最后 `fuser -k 11311/tcp` 再兜一层

    也就是说：会动其他包的 rosmaster，但只在 11311 这个端口上。
    如果车上有其他人正在用这个 master，他们会被踢掉——这是刻意的，
    因为我们马上就要自己起一个。
    """
    remote.run("pkill -INT -f 'roslaunch tianracer' || true", record=False)
    time.sleep(wait)
    remote.run(
        "pkill -9 -f 'roslaunch tianracer' || true; "
        "pkill -9 -f rosmaster || true; "
        "pkill -9 -f tianbot_core || true",
        record=False,
    )
    time.sleep(1)
    # 按端口清残留 master，避免 11311 被占用导致下次启动失败
    remote.run("fuser -k 11311/tcp 2>/dev/null || true", record=False)
    time.sleep(1)


# 电压抢订阅的输出文件。checks/voltage.py 也引用这个常量，
# 两边必须一致。runner 在启动前和收尾时各清一次，防止跨会话读到旧值。
VOLTAGE_TAP_PATH = "/tmp/tt_voltage.txt"


def is_master_running(remote: Remote) -> bool:
    result = remote.run(ros("rosnode list >/dev/null 2>&1 && echo UP"), record=False)
    return "UP" in result.stdout


def wait_for_master(remote: Remote, timeout: int = 40) -> bool:
    """等 rosmaster 真的开始监听。

    `start_launch` 返回时 roslaunch 刚 fork，rosmaster 还要 1~3 秒才监听 11311。
    在此之前跑 `rostopic echo` 会**立即**报 `Unable to communicate with master!`
    并退出，不是等待重试——tap 就白做了（Round 3 审查发现）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_master_running(remote):
            return True
        time.sleep(1)
    return False


def start_voltage_tap(remote: Remote, out_path: str = VOLTAGE_TAP_PATH) -> None:
    """在 rosmaster 就绪后抢订阅电压，把结果存到文件。

    调用前**必须**已经 wait_for_master() 成功。

    为什么需要这一步：如果 `/tianracer/voltage` 真的只发一次，
    晚订阅就必错过；早订阅在周期发布的情况下也不吃亏。
    但它是**补充手段**，checks/voltage.py 以现订阅为主。
    """
    # 先清旧文件，避免上次运行的残留值被当成本次结果
    remote.run(f"rm -f {out_path}", record=False)

    cmd = ros(
        f"( setsid nohup timeout 240 rostopic echo -n 1 /tianracer/voltage/data "
        f"> {out_path} 2>&1 < /dev/null & ) > /dev/null 2>&1; echo tapped"
    )
    try:
        remote.run(cmd, timeout=20, record=False)
    except Exception:
        pass


def start_launch(remote: Remote, package: str, launch_file: str,
                 extra_args: str = "", log_path: str = "/tmp/tt_launch.log") -> str:
    """后台启动一个 roslaunch，返回它的日志路径。

    这里踩过两个坑，均已在实车验证：

    1. 不加 `< /dev/null`，roslaunch 会继承 SSH 通道 stdin，exec_command 不返回。
    2. 只用 `& disown` 还不够——后台进程仍然继承了 stdout/stderr 管道，
       paramiko 的 stdout.read() 会一直阻塞到 PipeTimeout（实测 60 秒）。

    可靠写法：把启动动作封在子 shell 里 `( ... & )`，让父 shell 立即退出，
    同时把后台进程的三路 fd 全部重定向掉。
    """
    launcher = (
        f"cd ~ && setsid nohup roslaunch {package} {launch_file} {extra_args} "
        f"> {log_path} 2>&1 < /dev/null &"
    )
    cmd = ros(f"( {launcher} ) > /dev/null 2>&1 < /dev/null; echo launched")

    # 即使如此，加一层容错：超时也当作已启动，后续 wait_for_nodes 会验证
    try:
        remote.run(cmd, timeout=25, record=False)
    except Exception:
        pass
    return log_path


# bringup 启动后必须有这些节点。用名字而不是数量判断，
# 因为节点数会随版本变化，光数数会把「起来 8 个但缺关键的那个」当成正常。
REQUIRED_NODES = [
    "/tianracer/tianracer",
    "/robot_state_publisher",
    "/joint_state_publisher",
    "/tianracer/ekf_se",
    "/tianracer/complementary_filter_gain_node",
]

OPTIONAL_NODES = [
    "/tianracer/richbeam_lidar",
    "/tianracer/complementary_filter_gain_node",
    "/webrtc_ros_bridge",
]


def list_nodes(remote: Remote) -> List[str]:
    """当前所有节点名。master 没起时返回空列表。"""
    result = remote.run(ros("rosnode list 2>/dev/null"), record=False)
    return [n.strip() for n in result.stdout.splitlines()
            if n.strip().startswith("/")]


def wait_for_nodes(remote: Remote, timeout: int = 60) -> Dict[str, object]:
    """等必需节点全部出现。

    返回 {ready, nodes, missing, waited_sec}。
    中途就判断清楚缺哪个，不用干等满超时才告诉用户。
    """
    started = time.time()
    missing: List[str] = list(REQUIRED_NODES)
    nodes: List[str] = []
    while time.time() - started < timeout:
        nodes = list_nodes(remote)
        missing = [n for n in REQUIRED_NODES if n not in nodes]
        if not missing:
            return {
                "ready": True, "nodes": nodes, "missing": [],
                "waited_sec": round(time.time() - started, 1),
            }
        time.sleep(2)
    return {
        "ready": False, "nodes": nodes, "missing": missing,
        "waited_sec": round(time.time() - started, 1),
    }


def stop_launch(remote: Remote) -> None:
    """停止由本程序启动的 roslaunch。"""
    kill_all(remote, wait=4)
