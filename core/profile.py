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


# ---------------------------------------------------------------------------
# baseline 必需键（声明式校验）
# ---------------------------------------------------------------------------
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
        "hardware.vendor_id", "hardware.model_id",
        "hardware.expected_serial", "hardware.acceptable_serials",
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
        env_prefix=_resolve_env_prefix(ros_data, overrides),
        env_hook_file=str(ros_data.get("env_hook_file", "")),
        display_frames=[str(f) for f in (ros_data.get("display_frames") or [])],
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
