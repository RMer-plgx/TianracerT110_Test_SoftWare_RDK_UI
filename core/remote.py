"""ROS 环境管理：前缀、生命周期、进程清理。

这一层封装了 2026-10-30 排查中踩过的所有坑：

1. 非交互 shell 不加载 .bashrc 末尾的 ros1_setup()，必须显式 source。
2. devel/etc/catkin/profile.d/99.*.sh 的错误默认值会覆盖 ~/.bashrc
   的正确值，导致底盘打不开。这里在会话内临时覆盖，不改车辆文件。
3. bringup 退出不干净会残留 rosmaster 占住 11311 端口，
   下次启动直接失败。

**多产品改造（2026-11-03）**：本模块不再持有任何产品特定的常量。
工作空间、启动包、话题、节点名全部来自 `core.profile.Profile`。
函数签名一律带 profile 参数 —— 刻意不设默认值，
漏传会立刻报错，而不是悄悄用了 T110 的配置。
"""

from __future__ import annotations

import re

import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .profile import Profile
from .ssh import CommandResult, Remote

# 电压抢订阅的输出文件。checks/voltage.py 也引用这个常量，
# 两边必须一致。runner 在启动前和收尾时各清一次，防止跨会话读到旧值。
VOLTAGE_TAP_PATH = "/tmp/tt_voltage.txt"


def ros(cmd: str, profile: Profile) -> str:
    """给一条 ROS 命令加上环境前缀。"""
    if profile is None:
        raise ValueError(
            "ros() 需要 profile 参数。多产品重构后不存在「默认产品」——"
            "调用方必须显式传入 ctx.profile。"
        )
    return profile.ros_env_prefix() + cmd


def ros_env_prefix(profile: Profile) -> str:
    """兼容旧接口。等价于 profile.ros_env_prefix()。"""
    return profile.ros_env_prefix()


def source_only(profile: Profile) -> str:
    """只 source、不 export 的前缀。

    用于「读取车辆自身配置」的场景。若用了 ros()，会被我们
    自己 export 的正确值盖住，永远读不到车辆真实配置。
    """
    return (
        f"source /opt/ros/{profile.ros.distro}/setup.bash >/dev/null 2>&1; "
        f"source {profile.ros.workspace}/devel/setup.bash >/dev/null 2>&1; "
    )


@dataclass
class EnvVarDiff:
    name: str
    expected: str
    actual: Optional[str]
    known_deviation_reason: str = ""

    @property
    def matches(self) -> bool:
        return self.actual == self.expected


def read_bashrc_exports(remote: Remote, prefixes):
    """返回 {变量名: 值}；读不到返回 None（三态：有 / 空 / 读不到）。"""
    """读 `~/.bashrc` 里显式声明的产品环境变量。

    为什么不能直接读进程环境（read_env_vars）
    ---------------------------------------------
    `~/.bashrc` 开头有这段：

        case $- in
            *i*) ;;
              *) return;;      ← 非交互 shell 在这里就退出了
        esac

    而检测软件是通过 SSH 非交互执行命令的，所以 `.bashrc` 里后面的
    `export TIANRACER_BASE_PORT=/dev/ttyUSB0` **根本没执行**。
    此时 `source devel/setup.bash` 会用 env-hooks 的默认值填上
    （`/dev/tianbot_base` / 460800 / oradar_lidar）。

    实测（2026-11-13）：

        bash -ic 'echo $TIANRACER_BASE_PORT'   → /dev/ttyUSB0   （交互，正确）
        bash -c  'source setup.bash; echo ...' → /dev/tianbot_base（非交互，错）

    而车本身是好的 —— 用户交互登录时一切正常。所以「车辆标准」应该
    看 `.bashrc` 写了什么，而不是看非交五会话里被 env-hooks 覆盖后的值。
    （ADR-0012）

    只解析形如 `export NAME=value` 的行，忽略注释与函数体里的行。
    值两边的引号会去掉。

    `prefixes` 可以是单个前缀或多个。为什么要多个：T110 的变量既有
    `TIANRACER_*` 也有 `TIANBOT_NAME`，只按 env_prefix（TIANRACER）过滤
    会漏掉后者（2026-11-13 实车发现）。
    """
    if isinstance(prefixes, str):
        prefixes = [prefixes]
    pattern = "|".join(re.escape(p) for p in prefixes if p)
    if not pattern:
        return {}
    result = remote.run(
        f"test -f ~/.bashrc && "
        f"grep -E '^export ({pattern})[A-Z0-9_]*=' ~/.bashrc 2>/dev/null; "
        f"echo RC=$?",
        record=False,
    )
    # 三态：读不到 ≠ 没声明。
    # grep 在「一行都没匹配」时返回 1，这是正常的（车辆没声明）；
    # 但 `test -f` 失败（.bashrc 不存在）或 SSH 中断时也返回非 0，
    # 两者不分就会把「读不到」写成「车辆没声明任何变量」。
    text = result.stdout or ""
    if not result.ok or "RC=" not in text:
        return None            # 读不到，调用方应判 WARN 而不是下结论
    if "RC=2" in text:
        return None            # grep 出错
    vars_: Dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("export "):
            continue
        body = line[len("export "):]
        key, sep, value = body.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        # 去引号（支持 "x" 与 'x'）
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        # 去掉行尾注释（`=value  # comment`），但值内部可能有 #
        # 只在 # 前面是空白时认为是注释
        for i, ch in enumerate(value):
            if ch == "#" and i > 0 and value[i - 1] in " \t":
                value = value[:i].rstrip()
                break
        if key:
            vars_[key] = value
    return vars_


def read_env_vars(remote: Remote, profile: Profile) -> Dict[str, str]:
    """读取当前进程环境里的产品变量（前缀取自 profile）。

    **不能用 ros() 前缀**：ros_env_prefix 里已经 export 了
    BASE_PORT / BAUDRATE / LIDAR 的正确值，
    用它去读只会读到自己的修正结果，永远检不出车辆的真实配置偏差。
    （审查发现的漏洞：第 4 项永远 PASS，“已临时修正”永不触发。）
    """
    prefix = profile.env_prefix
    result = remote.run(source_only(profile) + f"env | grep '^{prefix}' | sort")
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

def kill_all(remote: Remote, profile: Profile, wait: int = 5) -> None:
    """清掉本产品的 roslaunch / rosmaster，确保端口释放。

    先 INT 让 roslaunch 正常收尾（它会自己 kill 子节点），再 KILL 兜底。

    注意匹配范围：
      - roslaunch 只按本产品的启动包名匹配，不会误杀其他包
      - rosmaster **会全杀**（它只是个名字），因为本工具要求独占 11311
      - 最后 `fuser -k 11311/tcp` 再兜一层

    也就是说：会动其他包的 rosmaster，但只在 11311 这个端口上。
    如果车上有其他人正在用这个 master，他们会被踢掉——这是刻意的，
    因为我们马上就要自己起一个。
    """
    package = profile.ros.launch_package
    remote.run(f"pkill -INT -f 'roslaunch {package}' || true", record=False)
    time.sleep(wait)
    remote.run(
        f"pkill -9 -f 'roslaunch {package}' || true; "
        "pkill -9 -f rosmaster || true; "
        "pkill -9 -f tianbot_core || true",
        record=False,
    )
    time.sleep(1)
    remote.run("fuser -k 11311/tcp 2>/dev/null || true", record=False)
    time.sleep(1)


def is_master_running(remote: Remote, profile: Profile) -> bool:
    result = remote.run(ros("rosnode list >/dev/null 2>&1 && echo UP", profile),
                        record=False)
    return "UP" in result.stdout


def wait_for_master(remote: Remote, profile: Profile, timeout: int = 40) -> bool:
    """等 rosmaster 真的开始监听。

    `start_launch` 返回时 roslaunch 刚 fork，rosmaster 还要 1~3 秒才监听 11311。
    在此之前跑 `rostopic echo` 会**立即**报 `Unable to communicate with master!`
    并退出，不是等待重试——tap 就白做了（Round 3 审查发现）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_master_running(remote, profile):
            return True
        time.sleep(1)
    return False


def start_voltage_tap(remote: Remote, profile: Profile,
                      out_path: str = VOLTAGE_TAP_PATH) -> None:
    """在 rosmaster 就绪后抢订阅电压，把结果存到文件。

    调用前**必须**已经 wait_for_master() 成功。
    """
    remote.run(f"rm -f {out_path}", record=False)

    topic = profile.ros.topic("voltage")
    cmd = ros(
        f"( setsid nohup timeout 240 rostopic echo -n 1 {topic} "
        f"> {out_path} 2>&1 < /dev/null & ) > /dev/null 2>&1; echo tapped",
        profile,
    )
    try:
        remote.run(cmd, timeout=20, record=False)
    except Exception:
        pass


def start_launch(remote: Remote, profile: Profile, extra_args: str = "",
                 log_path: str = "/tmp/tt_launch.log",
                 package: Optional[str] = None,
                 launch_file: Optional[str] = None) -> str:
    """后台启动产品的 roslaunch，返回它的日志路径。

    这里踩过两个坑，均已在实车验证：

    1. 不加 `< /dev/null`，roslaunch 会继承 SSH 通道 stdin，exec_command 不返回。
    2. 只用 `& disown` 还不够——后台进程仍然继承了 stdout/stderr 管道，
       paramiko 的 stdout.read() 会一直阻塞到 PipeTimeout（实测 60 秒）。

    可靠写法：把启动动作封在子 shell 里 `( ... & )`，让父 shell 立即退出，
    同时把后台进程的三路 fd 全部重定向掉。
    """
    # 允许覆盖：建图用 slam launch，不是 bringup
    package = package or profile.ros.launch_package
    launch_file = launch_file or profile.ros.launch_file
    launcher = (
        f"cd ~ && setsid nohup roslaunch {package} {launch_file} {extra_args} "
        f"> {log_path} 2>&1 < /dev/null &"
    )
    cmd = ros(f"( {launcher} ) > /dev/null 2>&1 < /dev/null; echo launched",
              profile)

    try:
        remote.run(cmd, timeout=25, record=False)
    except Exception:
        pass
    return log_path


def list_nodes(remote: Remote, profile: Profile) -> List[str]:
    """当前所有节点名。master 没起时返回空列表。"""
    result = remote.run(ros("rosnode list 2>/dev/null", profile), record=False)
    return [n.strip() for n in result.stdout.splitlines()
            if n.strip().startswith("/")]


def wait_for_nodes(remote: Remote, profile: Profile,
                   timeout: int = 60) -> Dict[str, object]:
    """等必需节点全部出现。

    返回 {ready, nodes, missing, waited_sec}。
    中途就判断清楚缺哪个，不用干等满超时才告诉用户。
    """
    started = time.time()
    required = list(profile.ros.nodes_required)
    missing: List[str] = list(required)
    nodes: List[str] = []
    while time.time() - started < timeout:
        nodes = list_nodes(remote, profile)
        missing = [n for n in required if n not in nodes]
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


def stop_launch(remote: Remote, profile: Profile) -> None:
    """停止由本程序启动的 roslaunch。"""
    kill_all(remote, profile, wait=4)
