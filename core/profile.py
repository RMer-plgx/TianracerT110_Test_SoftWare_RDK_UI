"""产品配置档（profile）：让同一套检测逻辑服务多个产品。

设计要点
--------
**产品差异全部集中在 products/<id>/ 目录里，代码不认具体产品。**

一个 profile 由两份文件构成：
    products/<id>/profile.yaml    产品定义：ROS 话题、工作空间、检测项清单
    products/<id>/baseline.yaml   判定阈值

为什么要分开：加新产品时只该改配置，不该改 .py。如果把阈值和话题
混在一个文件里，两份职责互相干扰，改阈值时容易碰坏话题。

关于「静默兜底」
----------------
本模块**刻意不做** `cfg.get("x", 默认值)` 这种兜底。缺字段就抛
`ProfileError` 并说清缺什么、在哪个文件。

原因（2026-11-03 审查重点）：如果 profile 里少写了某个阈值，
兜底会让检测项用默认值继续跑，报告看起来正常，实际判的却是
另一套标准。这种错误比崩溃危险得多 —— 崩溃会被发现，静默跑偏不会。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import resource_path

# 检测阶段。顺序即依赖：static 不需要 ROS，runtime 需要 bringup，
# motion 需要人确认安全。
PHASES = ("static", "runtime", "motion")


class ProfileError(Exception):
    """profile 配置有问题。消息必须能让人直接定位到哪个文件的哪个字段。"""


@dataclass
class TapConfig:
    """串口协议采集（第 20 项）的配置。

    可选段。缺省 ``enabled=False``。

    这里刻意用一个**显式开关**，而不是「看车上日志文件在不在」：
    第 20 项判的是「本次采集的链路质量」，而日志文件可能是上一次
    采集的残留。没有开关就无法区分两者，会把旧数据当本次结果判 PASS
    —— 那是伪验证，比 SKIP 更危险。

    缺字段不报错，与「阈值不兜底」的原则不冲突：阈值兜底会静默
    改变判定标准；而这里缺了只会让本项判 SKIP，是安全方向。
    """

    enabled: bool = False
    port_env: str = ""
    baudrate_env: str = ""
    log_dir: str = "/tmp/tap"
    max_log_mb: int = 32
    # 车端录音目录的环境变量名。车端补丁读这个变量决定往哪写。
    # 用 TIANBOT_ 前缀而不是产品的 TIANRACER_：tianbot_core 服务多种车型，
    # 录音是它的通用能力，不该绑在某个产品前缀上。
    dir_env: str = "TIANBOT_TAP_DIR"

    def log_path(self, name: str) -> str:
        """某个方向的日志绝对路径。"""
        return f"{self.log_dir.rstrip('/')}/{name}"


@dataclass
class RosConfig:
    """产品的 ROS 侧定义。"""

    distro: str
    workspace: str
    launch_package: str
    launch_file: str
    env_overrides: Dict[str, str]
    topics: Dict[str, str]
    nodes_required: List[str]
    nodes_optional: List[str]
    # SLAM / 建图链路用的 roslaunch
    slam_package: str = ""
    slam_file: str = ""
    # 地图文件所在目录（相对工作空间）
    maps_dir: str = ""
    # 参与「源码一致性」检查的仓库目录名（相对 <workspace>/src）
    repos: List[str] = field(default_factory=list)
    # 交付镜像自带的车端改动所在分支。{仓库名: 分支名}。
    #
    # 背景：第 7 项把「本地分支未推送」判 FAIL，因为换一台车就复现不出来。
    # 但车端补丁（串口录音）本身就是装在本地分支上的，这类分支在每个
    # 已装补丁的车上都未推送 —— 全判 FAIL 会让整条产品线永远不可交付，
    # 第 7 项也就没人看了。
    #
    # 所以：声明过的分支单列一行、判 WARN（可见但不阻断）；
    # 没声明的分支仍判 FAIL。可复现的要求还在，只是把「已知的交付内容」
    # 与「随手改的本地分支」区分开。
    #
    # 写错仓库名不生效，会回到 FAIL —— 不做事后猜测。
    allowed_local_branches: Dict[str, str] = field(default_factory=dict)

    # 已确认等价的本地/上游分叉（2026-11-13，ADR-0009 后续）。
    # 背景：车上 tianbot_core 与远端 dev 各自实现了 raw mode 修复 + 串口录音，
    # 内容等价（差异只有注释语言），但 git 层面是「领先 2 / 落后 3」。
    # 第 7 项会把这种分叉报成 WARN，让人以为车上是老代码。
    #
    # 声明后：本地领先的提交 ⊆ local_commits 且落后的 ⊆ remote_commits
    #        → 判 PASS「已确认等价分叉」
    # 出现任何不在列表里的新提交 → 仍报 WARN（上游有新东西 / 本地有新改动）。
    #
    # 形如：{仓库名: {reason, verified_at, local_commits, remote_commits}}
    known_divergences: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # 第 7 项的「已知可接受」项（2026-11-13）。
    # accept_unreachable: remote 不可达但已知可接受的仓库（如 SSH 无 key）。
    # accept_dirty: 未提交改动属于现场适配，不作为告警。
    # 两者都只是「降噪」—— 具体情况仍会逐条写进报告 detail，
    # 不会隐藏任何信息。
    accept_unreachable: List[str] = field(default_factory=list)
    accept_dirty: bool = False
    # 报告里要额外展示父节点的 frame（可选）。
    # 这些是「看着有用」而非「必需」的，缺了不报错，只是不显示。
    display_frames: List[str] = field(default_factory=list)
    # env-hooks 文件路径（相对工作空间）。用于「根本修复方式」文案，
    # 留空则报告不提这一句 —— 不猜不存在的文件。
    env_hook_file: str = ""
    # 环境变量前缀，例如 TIANRACER。
    # 用于「读取车辆自身环境变量」时按前缀过滤（第 4 项）。
    # 可在 profile.yaml 的 ros.env_prefix 显式声明；
    # 未声明时由 env_overrides 的键推导公共前缀。
    env_prefix: str = ""
    # 串口协议采集（第 20 项）。可选，默认关闭。
    tap: TapConfig = field(default_factory=TapConfig)

    def topic(self, key: str) -> str:
        """取话题名。缺了就报错，不用默认值糊过去。"""
        try:
            return self.topics[key]
        except KeyError:
            raise ProfileError(
                f"profile.yaml 里缺少 ros.topics.{key}。\n"
                f"已定义的话题：{', '.join(sorted(self.topics)) or '（无）'}"
            ) from None


@dataclass
class CheckSpec:
    """一个检测项的定义。"""

    number: int
    title: str
    module: str
    phase: str
    fatal: bool = False
    # 该项依赖的前置项编号（比如 IMU 依赖 bringup 起来）
    requires: List[int] = field(default_factory=list)

    def as_registry_tuple(self):
        """转成 runner 原来用的元组格式，减少改动面。"""
        return (self.number, self.title, self.module, "run",
                self.fatal, self.phase)


@dataclass
class Profile:
    """一个完整的产品配置。"""

    id: str
    name: str
    model: str
    platform: str
    description: str
    ros: RosConfig
    checks: List[CheckSpec]
    baseline: Dict[str, Any]
    source_dir: Path
    # 版本号，进报告便于追溯「这份结果是用哪套配置跑的」
    revision: str = ""
    # 环境变量分组说明（可选，多行）。多产品后不能再把 T110 的
    # 变量清单写死在 checks/envvars.py 里，改由产品自己声明。
    env_note: str = ""

    # -- 便捷访问 ---------------------------------------------------------

    def registry(self) -> List[tuple]:
        """给 runner 用的检测项清单，保持配置里的顺序。"""
        return [spec.as_registry_tuple() for spec in self.checks]

    def check_numbers(self) -> List[int]:
        return [spec.number for spec in self.checks]

    def fatal_numbers(self) -> List[int]:
        return [spec.number for spec in self.checks if spec.fatal]

    def title_of(self, number: int) -> str:
        for spec in self.checks:
            if spec.number == number:
                return spec.title
        return f"第 {number} 项"

    @property
    def total_checks(self) -> int:
        return len(self.checks)

    @property
    def env_prefix(self) -> str:
        """环境变量前缀，例如 TIANRACER。空串表示本产品不涉及环境变量。"""
        return self.ros.env_prefix

    def env_export_lines(self) -> List[str]:
        """生成 export 语句，顺序与原实现一致。"""
        return [f"export {name}={value}"
                for name, value in self.ros.env_overrides.items()]

    def ros_env_prefix(self, master_uri: str = "http://localhost:11311") -> str:
        """拼出「source 环境 + export 正确值」的家用前缀。

        与原 `ROS_ENV_PREFIX` 语义一致：
          1. source ROS 与工作空间
          2. 设 ROS_MASTER_URI / ROS_IP
          3. 覆盖 env-hooks 的错误默认值
        """
        parts = [
            f"source /opt/ros/{self.ros.distro}/setup.bash >/dev/null 2>&1",
            f"source {self.ros.workspace}/devel/setup.bash >/dev/null 2>&1",
            f"export ROS_MASTER_URI={master_uri}",
            # 多网卡机器上要让 ROS 知道对端能访问哪个地址
            "export ROS_IP=$(hostname -I | awk '{print $1}')",
            "export DISABLE_ROS1_EOL_WARNINGS=True",
        ]
        parts.extend(self.env_export_lines())
        # 车端录音目录单独注入，**不放 env_overrides**。
        # env_overrides 的语义是「纠正 env-hooks 的产品变量」，
        # 会被第 4 项环境变量检查当作白名单比对；把录音开关混进去
        # 会让那一项报「变量被改过」的假告警。
        tap = getattr(self.ros, "tap", None)
        if tap is not None and tap.enabled and tap.dir_env:
            parts.append(f"export {tap.dir_env}={tap.log_dir}")
        return "; ".join(parts) + "; "

    def baseline_section(self, key: str) -> Dict[str, Any]:
        """取 baseline 的一个顶层节。缺失时报错而不是返回空字典。"""
        value = self.baseline.get(key)
        if value is None:
            raise ProfileError(
                f"baseline.yaml 里缺少 `{key}` 节。\n"
                f"文件：{self.source_dir / 'baseline.yaml'}"
            )
        return value


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------

def _resolve_env_prefix(ros_data: Dict[str, Any],
                        overrides: Dict[str, Any]) -> str:
    """确定环境变量前缀。

    优先用 profile.yaml 里显式写的 ros.env_prefix；
    没写就从 env_overrides 的键推导公共前缀（到最后一个下划线）。

    为什么要推导：每个产品的变量前缀不同（TIANRACER_* / ROBOT_*），
    第 4 项按前缀 `env | grep '^PREFIX'` 过滤，前缀错了会读不到任何变量，
    报告会误判成「车辆没有配置环境变量」。
    """
    explicit = ros_data.get("env_prefix")
    if explicit:
        return str(explicit).strip()

    names = [str(k) for k in overrides]
    if not names:
        # 没有任何 env_overrides：很多产品确实不配环境变量。
        # 返回空串，由第 4 项自己判断「本产品不适用」并判 SKIP，
        # 而不是让整个程序启动失败。
        return ""
    if len(names) == 1:
        head = names[0].split("_")[0]
        return head

    common = names[0]
    for name in names[1:]:
        while not name.startswith(common):
            common = common[:-1]
            if not common:
                break
    # 截到最后一个下划线之前
    if "_" in common:
        common = common[: common.rindex("_")]
    if not common:
        raise ProfileError(
            "无法从 env_overrides 的键推导出公共前缀："
            f"{', '.join(sorted(names)[:5])}。\n"
            "请在 profile.yaml 显式写 ros.env_prefix。"
        )
    return common


def _build_allowed_branches(raw: Any, where: str) -> Dict[str, str]:
    """解析 ros.allowed_local_branches：交付镜像自带的车端改动。

    形如 {tianbot_core: feat/serial-tap-recording}。缺省为空，
    即「本产品不预先认可任何本地分支」—— 默认从严。

    **严格校验类型**：这行配置直接决定第 7 项判 FAIL 还是 WARN。
    静默把 `None` / `123` / `[a, b]` 转成字符串会让用户以为声明生效了，
    而实际匹配不上（然后看见一句莫名其妙的「声明的是 `None`，不生效」）。
    最常见的写法错误就是在 YAML 里写了键忘了写值 —— 必须当场报错。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ProfileError(
            f"{where} 的 allowed_local_branches 应为映射（仓库名: 分支名），"
            f"实际是 {type(raw).__name__}"
        )
    out: Dict[str, str] = {}
    for repo, branch in raw.items():
        if not isinstance(repo, str) or not repo.strip():
            raise ProfileError(
                f"{where} 的 allowed_local_branches 里有一个键不是非空字符串："
                f"{repo!r}"
            )
        if not isinstance(branch, str):
            raise ProfileError(
                f"{where} 的 allowed_local_branches 里 `{repo}` 的值应为分支名"
                f"字符串，实际是 {type(branch).__name__}（{branch!r}）。\n"
                f"常见原因：YAML 里写了仓库名但忘了写分支名。"
            )
        if not branch.strip():
            raise ProfileError(
                f"{where} 的 allowed_local_branches 里 `{repo}` 的分支名为空"
            )
        out[repo] = branch.strip()
    return out


def _build_known_divergences(raw: Any, where: str) -> Dict[str, Dict[str, Any]]:
    """解析 ros.known_divergences：已确认等价的本地/上游分叉。

    严格校验：写错了会导致第 7 项误判 PASS 或误判 WARN，
    比「静默忽略」危险得多。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ProfileError(
            f"{where} 的 known_divergences 应为映射（仓库名: 说明），"
            f"实际是 {type(raw).__name__}"
        )
    out: Dict[str, Dict[str, Any]] = {}
    for repo, info in raw.items():
        if not isinstance(repo, str) or not repo.strip():
            raise ProfileError(f"{where} 的 known_divergences 里有非法仓库名：{repo!r}")
        if not isinstance(info, dict):
            raise ProfileError(
                f"{where} 的 known_divergences.{repo} 应为映射，"
                f"至少含 reason 与 local_commits/remote_commits"
            )
        local = info.get("local_commits") or []
        remote = info.get("remote_commits") or []
        if not isinstance(local, list) or not isinstance(remote, list):
            raise ProfileError(
                f"{where} 的 known_divergences.{repo} 的 "
                f"local_commits / remote_commits 应为列表"
            )
        # 允许写短 sha（7 位）或全 sha；统一成小写、去空白
        def _norm(xs):
            return {str(x).strip().lower() for x in xs if str(x).strip()}
        out[repo] = {
            "reason": str(info.get("reason") or ""),
            "verified_at": str(info.get("verified_at") or ""),
            "local_commits": _norm(local),
            "remote_commits": _norm(remote),
            # 比对基准分支（可选）。不写就按本地 upstream / 同名远端分支。
            # 写它的理由：车上的分支是本地改造分支（feat/xxx），
            # 而评等价应该跟上游主干（dev）比，不是跟自己的镜像分支比。
            "compare_with": str(info.get("compare_with") or "").strip(),
        }
    return out


def _build_tap(raw: Any, where: str) -> TapConfig:
    """解析 ros.tap 段。缺省整个段等于未启用。

    只做类型校验，不做“猜”：写错了就报错，不静默当成关闭。
    静默兜底在这里特别危险 —— 用户以为开了采集，第 20 项却因配置
    写错而判 SKIP，报告上看不出区别。
    """
    if raw is None:
        return TapConfig()
    if not isinstance(raw, dict):
        raise ProfileError(f"{where} 的 tap 应为映射")

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ProfileError(
            f"{where} 的 tap.enabled 应为 true/false，实际是 {enabled!r}"
        )

    max_log_mb = raw.get("max_log_mb", 32)
    try:
        max_log_mb = int(max_log_mb)
    except (TypeError, ValueError):
        raise ProfileError(
            f"{where} 的 tap.max_log_mb 应为整数，实际是 {max_log_mb!r}"
        ) from None

    log_dir = str(raw.get("log_dir") or "/tmp/tap")
    if not log_dir.startswith("/"):
        raise ProfileError(
            f"{where} 的 tap.log_dir 应为绝对路径，实际是 {log_dir!r}"
        )

    return TapConfig(
        enabled=enabled,
        port_env=str(raw.get("port_env") or ""),
        baudrate_env=str(raw.get("baudrate_env") or ""),
        log_dir=log_dir,
        max_log_mb=max_log_mb,
    )


# ---------------------------------------------------------------------------
# baseline 必需键（声明式校验）# ---------------------------------------------------------------------------
# 为什么要有这张表
# ----------------
# checks/ 里到处是 `cfg.get("min", 45.0)` 这种写法。默认值本来是"兜底"，
# 但在多产品架构下它是个陷阱：baseline.yaml 少写一个键，检测项会安静地
# 用默认值继续跑，报告看起来正常，实际判的是另一套标准 —— 而没人知道。
#
# 逐个改掉 76 处调用点风险大、收益低。更好的办法是在**加载时**校验：
# 只要这些键都在，代码里的默认分支就永远走不到。
#
# 只校验「本产品启用了的检测项」需要的键 —— 没启用 lidar 的产品
# 不需要 radar 配置，不能因为没写就拒绝启动。
#
# 路径用点号分隔，例如 "frequency.imu_hz.min"。
BASELINE_REQUIREMENTS: Dict[str, List[str]] = {
    "camera": [
        "frequency.camera_hz.min", "frequency.camera_hz.max",
        "camera.expected_width", "camera.expected_height",
        "camera.expected_encoding",
    ],
    "hardware": [
        # 2026-11-13（ADR-0010）：不再比对 CP2102 的序列号，
        # 改为「平台关键词 + device-tree 序列号」。
        # vendor_id/model_id 保留：它们只验证「底盘是 CP2102」这个拓扑事实。
        "hardware.vendor_id", "hardware.model_id",
    ],
    "health": [
        "health.temp_warn_c", "health.temp_fail_c",
        "health.disk_warn_pct", "health.disk_fail_pct",
        "health.mem_available_min_gib", "health.load_warn",
    ],
    "imu": [
        "frequency.imu_hz.min", "frequency.imu_hz.max",
        "imu.accel_z_min", "imu.accel_z_max", "imu.accel_z_warn_high",
        "imu.gyro_static_max", "imu.roll_pitch_max_deg",
    ],
    "lannet": [
        "network.lidar_ip", "network.lidar_host_ip_prefix",
        "network.ping_count", "network.ping_max_loss_pct",
        "network.ping_max_rtt_ms",
    ],
    "lidar": [
        "frequency.radar_hz.min", "frequency.radar_hz.max",
        "radar.expected_points", "radar.angle_tolerance_deg",
        "radar.angle_max_deg", "radar.min_valid_ratio",
    ],
    "mapping": [
        "mapping.expected_resolution", "mapping.expected_map_size",
    ],
    "motion": [
        "motion.micro_pulse.speed", "motion.micro_pulse.duration",
        "motion.micro_pulse.min_displacement_m",
        "motion.bench.forward_speed", "motion.bench.forward_duration",
        "motion.bench.reverse_speed", "motion.bench.reverse_duration",
        "motion.bench.min_displacement_m",
        "motion.bench.steer_angle", "motion.bench.steer_duration",
        "motion.bench.steer_speed",
        "motion.ground.forward_speed", "motion.ground.forward_duration",
        "motion.ground.forward_expected_m", "motion.ground.forward_tolerance",
        "motion.ground.reverse_speed", "motion.ground.reverse_duration",
        "motion.ground.reverse_expected_m", "motion.ground.reverse_tolerance",
        "motion.ground.steer_angle", "motion.ground.steer_duration",
        "motion.ground.steer_expected_yaw_deg",
        "motion.ground.steer_tolerance_deg",
        "motion.spin.speed", "motion.spin.steer_angle",
        "motion.spin.target_yaw_deg", "motion.spin.max_seconds",
        "motion.spin.yaw_tolerance_deg", "motion.spin.yaw_warn_deg",
        "motion.spin.closure_warn_m",
        "motion.safety.max_speed_mps", "motion.safety.max_duration_sec",
        "motion.safety.spin_max_speed_mps", "motion.safety.spin_max_seconds",
    ],
    "odom": [
        "frequency.odom_hz.min", "frequency.odom_hz.max",
        "odom.static_drift_max_m", "odom.sample_seconds",
    ],
    "tf": [
        "tf.required_frames", "tf.required_transforms",
    ],
    "traces": [
        "traces.ros_log_size_warn_mb", "traces.bash_history_lines_warn",
    ],
    "voltage": [
        "voltage.full_v", "voltage.normal_v", "voltage.low_v",
        "voltage.stop_test_below_v", "voltage.sample_timeout_sec",
    ],
}
# 其余模块（network / identity / rustdesk / envvars / devices / source / teb）
# 不读阈值，无需声明。


def _lookup_path(data: Dict[str, Any], dotted: str) -> tuple:
    """按点分路径取值，返回 (是否找到, 值)。"""
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _validate_baseline(specs, baseline: Dict[str, Any],
                       baseline_path: Path) -> None:
    """校验启用检测项所需的 baseline 键都存在。

    一次性报出**所有**缺失项，而不是遇到第一个就抛 ——
    配置写错时用户想一次看全，不想改一个跑一次。
    """
    missing: List[str] = []
    for spec in specs:
        for path in BASELINE_REQUIREMENTS.get(spec.module, []):
            found, _ = _lookup_path(baseline, path)
            if not found:
                missing.append(f"  {path}    （第 {spec.number} 项 {spec.title} 需要）")

    if missing:
        raise ProfileError(
            f"{baseline_path.name} 缺少以下必需键（共 {len(missing)} 处）：\n"
            + "\n".join(missing)
            + f"\n\n文件：{baseline_path}\n"
            "这些不是可选项 —— 代码里有同名的默认值，但那是给"
            "「读不到数据」用的兜底，不该由配置缺失来触发。"
        )


def _validate_env_var_config(specs, baseline: Dict[str, Any],
                             ros: "RosConfig", where: str) -> None:
    """环境变量配置的自洽性。

    两种合法情形：
      · 没前缀 + 没有期望值 → 第 4 项判 SKIP（本产品不涉及）
      · 有前缀 → 正常比对

    不合法：配了期望值却没有前缀 —— 那第 4 项永远读不到，
    每次都报「不符」，实际是配置本身矛盾。
    """
    wants_env_check = any(spec.module == "envvars" for spec in specs)
    if not wants_env_check:
        return

    expected = baseline.get("envvars") or {}
    if not ros.env_prefix and expected:
        raise ProfileError(
            f"{where} 启用了「环境变量」检测，baseline.envvars 也写了 "
            f"{len(expected)} 项期望值，\n"
            f"但 ros.env_prefix 为空 —— 无法按前缀读取，第 4 项永远不可能通过。\n"
            f"请补 ros.env_prefix，或从 checks.items 里去掉 envvars。"
        )


def _require(data: Dict[str, Any], key: str, where: str) -> Any:
    """取必填字段。缺失时报出完整路径。"""
    if key not in data or data[key] in (None, ""):
        raise ProfileError(f"{where} 缺少必填字段 `{key}`")
    return data[key]


def products_root() -> Path:
    """products 目录的位置。

    打包后位于 PyInstaller 的解压目录，resource_path 已处理。
    """
    return resource_path("products")


def list_profiles() -> List[str]:
    """列出所有可用的产品 id（跳过 _template 与隐藏目录）。"""
    root = products_root()
    if not root.is_dir():
        return []
    return sorted(
        item.name for item in root.iterdir()
        if item.is_dir()
        and not item.name.startswith(("_", "."))
        and (item / "profile.yaml").is_file()
    )


def default_profile_id() -> str:
    """默认产品。

    优先读 products/_default.txt（部署时写一行产品 id），
    没有就用列表里第一个。这样换默认产品不用改代码。
    """
    marker = products_root() / "_default.txt"
    if marker.is_file():
        value = marker.read_text(encoding="utf-8").strip()
        if value:
            return value
    available = list_profiles()
    if not available:
        raise ProfileError(
            f"没有找到任何产品配置。\n"
            f"请确认 {products_root()} 下至少有一个含 profile.yaml 的目录。"
        )
    return available[0]


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise ProfileError(
            "缺少 PyYAML。请先执行：pip install -r requirements.txt"
        ) from exc

    if not path.is_file():
        raise ProfileError(f"找不到配置文件：{path}")

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ProfileError(
            f"{path.name} 不是合法的 YAML：{type(exc).__name__}: {exc}\n"
            f"文件：{path}"
        ) from exc

    if not isinstance(data, dict):
        raise ProfileError(f"{path.name} 顶层应为映射（dict），实际是 {type(data).__name__}")
    return data


def load(profile_id: Optional[str] = None) -> Profile:
    """加载一个产品配置。

    出错一律抛 ProfileError，消息里带文件路径和字段名 ——
    用户看到的不该是 `KeyError: 'topics'`。
    """
    pid = profile_id or default_profile_id()
    source_dir = products_root() / pid
    if not source_dir.is_dir():
        available = list_profiles()
        raise ProfileError(
            f"产品 `{pid}` 不存在。\n"
            f"可用产品：{', '.join(available) or '（无）'}\n"
            f"目录：{products_root()}"
        )

    profile_path = source_dir / "profile.yaml"
    baseline_path = source_dir / "baseline.yaml"

    data = _load_yaml(profile_path)
    baseline = _load_yaml(baseline_path)

    # ---- 基本信息 ----
    where = f"{profile_path.name}"
    name = _require(data, "name", where)
    ros_data = data.get("ros")
    if not isinstance(ros_data, dict):
        raise ProfileError(f"{where} 缺少 `ros` 节，或它不是映射")

    ros_where = f"{where} 的 ros 节"
    launch = ros_data.get("launch") or {}
    if not isinstance(launch, dict):
        raise ProfileError(f"{ros_where} 的 launch 应为映射")

    topics = ros_data.get("topics") or {}
    if not isinstance(topics, dict) or not topics:
        raise ProfileError(
            f"{ros_where} 缺少 `topics`，或它是空的。\n"
            f"至少需要定义 odom / imu / scan / cmd 等话题。"
        )

    nodes = ros_data.get("nodes") or {}
    overrides = ros_data.get("env_overrides") or {}
    if not isinstance(overrides, dict):
        raise ProfileError(f"{ros_where} 的 env_overrides 应为映射（变量名: 值）")

    slam = ros_data.get("slam") or {}
    if not isinstance(slam, dict):
        raise ProfileError(f"{ros_where} 的 slam 应为映射")

    ros = RosConfig(
        distro=str(ros_data.get("distro", "noetic")),
        workspace=str(_require(ros_data, "workspace", ros_where)),
        launch_package=str(_require(launch, "package", f"{ros_where}.launch")),
        launch_file=str(_require(launch, "file", f"{ros_where}.launch")),
        env_overrides={str(k): str(v) for k, v in overrides.items()},
        topics={str(k): str(v) for k, v in topics.items()},
        nodes_required=[str(n) for n in (nodes.get("required") or [])],
        nodes_optional=[str(n) for n in (nodes.get("optional") or [])],
        slam_package=str(slam.get("package", "")),
        slam_file=str(slam.get("file", "")),
        maps_dir=str(ros_data.get("maps_dir", "")),
        repos=[str(r) for r in (ros_data.get("repos") or [])],
        allowed_local_branches=_build_allowed_branches(
            ros_data.get("allowed_local_branches"), ros_where),
        known_divergences=_build_known_divergences(
            ros_data.get("known_divergences"), ros_where),
        accept_unreachable=[str(x) for x in (ros_data.get("accept_unreachable") or [])],
        accept_dirty=bool(ros_data.get("accept_dirty") or False),
        env_prefix=_resolve_env_prefix(ros_data, overrides),
        env_hook_file=str(ros_data.get("env_hook_file", "")),
        display_frames=[str(f) for f in (ros_data.get("display_frames") or [])],
        tap=_build_tap(ros_data.get("tap"), ros_where),
    )

    # ---- 检测项 ----
    checks_data = data.get("checks") or {}
    items = checks_data.get("items")
    if not isinstance(items, list) or not items:
        raise ProfileError(
            f"{where} 缺少 `checks.items`，或它是空的。\n"
            f"这里要列出本产品要跑哪些检测项、什么顺序。"
        )

    specs: List[CheckSpec] = []
    seen_numbers = set()
    seen_modules = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ProfileError(f"{where} 的 checks.items[{index}] 应为映射")
        item_where = f"{where} 的 checks.items[{index}]"
        try:
            number = int(_require(item, "number", item_where))
            title = str(_require(item, "title", item_where))
            module = str(_require(item, "module", item_where))
            phase = str(_require(item, "phase", item_where))
        except (TypeError, ValueError) as exc:
            raise ProfileError(f"{item_where} 字段类型不对：{exc}") from exc

        if phase not in PHASES:
            raise ProfileError(
                f"{item_where} 的 phase=`{phase}` 无效。"
                f"只能是 {' / '.join(PHASES)}。"
            )
        if number in seen_numbers:
            raise ProfileError(f"{item_where} 编号 {number} 重复")
        if module in seen_modules:
            raise ProfileError(f"{item_where} 模块 `{module}` 重复")
        seen_numbers.add(number)
        seen_modules.add(module)

        specs.append(CheckSpec(
            number=number,
            title=title,
            module=module,
            phase=phase,
            fatal=bool(item.get("fatal", False)),
            requires=[int(x) for x in (item.get("requires") or [])],
        ))

    # ---- 跨字段校验 ----
    _validate_ordering(specs, where)
    _validate_requires(specs, where)
    _validate_baseline(specs, baseline, baseline_path)
    _validate_env_var_config(specs, baseline, ros, where)

    return Profile(
        id=pid,
        name=str(name),
        model=str(data.get("model", name)),
        platform=str(data.get("platform", "")),
        description=str(data.get("description", "")),
        ros=ros,
        checks=specs,
        baseline=copy.deepcopy(baseline),
        source_dir=source_dir,
        revision=str(data.get("revision", "")),
        env_note=str(data.get("env_note", "") or ""),
    )


def _validate_ordering(specs: List[CheckSpec], where: str) -> None:
    """阶段顺序必须是不回头的 static → runtime → motion。

    为什么要在加载时校验：执行流程按配置顺序跑，如果配置写成
    static → motion → runtime，运动测试会在 ROS 还没起来时执行，
    结果是「无订阅者」而不是「顺序错了」—— 症状和原因离得很远。
    """
    ranks = {phase: index for index, phase in enumerate(PHASES)}
    previous_rank = -1
    for spec in specs:
        rank = ranks[spec.phase]
        if rank < previous_rank:
            raise ProfileError(
                f"{where} 的检测项顺序不对：第 {spec.number} 项（{spec.title}）"
                f"是 {spec.phase} 阶段，却排在 {PHASES[previous_rank]} 阶段之后。\n"
                f"正确顺序：{' → '.join(PHASES)}，同一阶段内可任意排列。"
            )
        previous_rank = rank


def _validate_requires(specs: List[CheckSpec], where: str) -> None:
    """requires 引用的编号必须真实存在，且不能指向自己。"""
    numbers = {spec.number for spec in specs}
    for spec in specs:
        for dep in spec.requires:
            if dep == spec.number:
                raise ProfileError(f"{where} 第 {spec.number} 项不能 requires 自己")
            if dep not in numbers:
                raise ProfileError(
                    f"{where} 第 {spec.number} 项 requires 了不存在的第 {dep} 项"
                )


def load_safe(profile_id: Optional[str] = None) -> tuple:
    """加载并返回 (profile, error_message)。不抛异常，供界面兜底展示。"""
    try:
        return load(profile_id), ""
    except ProfileError as exc:
        return None, str(exc)
