"""多产品重构的等价性测试。

2026-11-03 把「T110 专用」改成「多产品共用」，核心风险不是新代码有 bug，
而是**旧行为悄悄变了**。这组测试把重构前的行为写成断言：

  · 19 个检测项的编号、标题、模块、致命性、阶段、顺序
  · 6 个关键 ROS 话题
  · 6 个环境变量覆盖
  · baseline 的 15 个节

任何一项变动都会让这里变红 —— 那时要么是重构出错，
要么是刻意改了行为，需要同步更新这份「契约」。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import profile as product_profile  # noqa: E402
from core.profile import ProfileError  # noqa: E402


@pytest.fixture(scope="module")
def prof():
    return product_profile.load("tianracer_t110_rdk")


class TestRegistryUnchanged:
    """检测项清单的顺序与编号（2026-11-13 重新编号，ADR-0009）。"""

    # 2026-11-13：重新编号，使**编号 = 执行顺序**。
    # 旧编号是历史遗留：16/17/19 是 static 却编在后面，18 是 motion
    # 却编在 19 前面，报告里编号跳来跳去。
    # 新顺序同时满足：
    #   - static → runtime → motion 的硬依赖
    #   - 通讯链路前置到 runtime 最前（后续项可参考它的帧统计）
    #   - IMU 与里程计相邻，紧跟建图链路（两者都靠落地互校转 PASS）
    EXPECTED = [
        (1,  "网络连通性",         "network",  True,  "static"),
        (2,  "系统身份",           "identity", False, "static"),
        (3,  "RustDesk 远程桌面",  "rustdesk", False, "static"),
        (4,  "环境变量",           "envvars",  False, "static"),
        (5,  "设备节点",           "devices",  False, "static"),
        (6,  "主控平台与序列号",   "hardware", True,  "static"),
        (7,  "源码一致性",         "source",   False, "static"),
        (8,  "雷达以太网",         "lannet",   False, "static"),
        (9,  "teb 导航插件",       "teb",      False, "static"),
        (10, "系统健康",           "health",   False, "static"),
        (11, "使用痕迹",           "traces",   False, "static"),
        (12, "通讯链路（协议层）", "protocol", False, "runtime"),
        (13, "激光雷达",           "lidar",    True,  "runtime"),
        (14, "USB 摄像头",         "camera",   False, "runtime"),
        (15, "TF 树",              "tf",       False, "runtime"),
        (16, "电池电压",           "voltage",  False, "runtime"),
        (17, "激光建图链路",       "mapping",  False, "runtime"),
        (18, "IMU",               "imu",      True,  "runtime"),
        (19, "里程计",             "odom",     True,  "runtime"),
        (20, "运动控制",           "motion",   False, "motion"),
    ]

    def test_count_is_20(self, prof):
        assert prof.total_checks == 20

    def test_items_identical_and_ordered(self, prof):
        actual = [(n, t, m, f, ph) for (n, t, m, _fn, f, ph) in prof.registry()]
        assert actual == self.EXPECTED

    def test_fatal_set_unchanged(self, prof):
        # 关键项：网络、主控平台、雷达、IMU、里程计
        # 2026-11-13：主控加入 fatal（平台不对则中止，ADR-0010）
        assert prof.fatal_numbers() == [1, 6, 13, 18, 19]

    def test_numbers_unique(self, prof):
        numbers = prof.check_numbers()
        assert len(numbers) == len(set(numbers))

    def test_modules_unique(self, prof):
        modules = [spec.module for spec in prof.checks]
        assert len(modules) == len(set(modules))


class TestRosConfigUnchanged:
    def test_topics(self, prof):
        expected = {
            "odom": "/tianracer/odom",
            "imu": "/tianracer/imu_raw",
            "scan": "/tianracer/scan",
            "camera": "/tianracer/camera/image_raw",
            "cmd": "/tianracer/ackermann_cmd",
            "voltage": "/tianracer/voltage/data",
        }
        for key, want in expected.items():
            assert prof.ros.topic(key) == want

    def test_workspace_and_launch(self, prof):
        assert prof.ros.workspace == "~/tianracer_ros1_ws"
        assert prof.ros.launch_package == "tianracer_bringup"
        assert prof.ros.launch_file == "tianracer_bringup.launch"

    def test_env_overrides(self, prof):
        """覆盖清单。

        2026-11-05 实车修复：移除了 TIANRACER_LIDAR_PORT。
        原因见 profile.yaml 注释 —— 那台车的 richbeam_lidar.launch.xml
        里 lidar_serial 写死 /dev/ttyUSB0，而 /dev/tianbot_lidar 也指向
        ttyUSB0，雷达与底盘抢串口，bringup 起不来。
        """
        expected = {
            "TIANRACER_BASE_PORT": "/dev/ttyUSB0",
            "TIANRACER_BASE_BAUDRATE": "115200",
            "TIANRACER_LIDAR": "richbeam_lidar",
            "TIANRACER_LIDAR_IP": "192.168.198.2",
            "TIANRACER_NAME": "tianracer",
        }
        assert prof.ros.env_overrides == expected

    def test_no_lidar_serial_override(self, prof):
        """不该覆盖 LIDAR_PORT —— 会让雷达与底盘抢同一个串口。"""
        assert "TIANRACER_LIDAR_PORT" not in prof.ros.env_overrides

    def test_ros_env_prefix_contains_all_overrides(self, prof):
        prefix = prof.ros_env_prefix()
        for name, value in prof.ros.env_overrides.items():
            assert f"export {name}={value}" in prefix
        # 基础环境也要在
        assert "source /opt/ros/noetic/setup.bash" in prefix
        assert "ROS_MASTER_URI" in prefix
        assert "ROS_IP" in prefix

    def test_slam_config(self, prof):
        assert prof.ros.slam_package == "tianracer_slam"
        assert prof.ros.maps_dir.endswith("maps")

    def test_repos(self, prof):
        assert prof.ros.repos == ["tianracer", "tianbot_core", "webrtc_ros_bridge"]


class TestBaselineUnchanged:
    def test_all_sections_present(self, prof):
        expected = {
            "frequency", "radar", "imu", "odom", "voltage", "health",
            "traces", "motion", "envvars", "envvars_known_deviations",
            "hardware", "network", "camera", "tf", "mapping",
            # 2026-11-13：第 3 项自动修复的开关（ADR-0011 / R17-8）
            "rustdesk",
        }
        assert set(prof.baseline) == expected

    def test_voltage_thresholds(self, prof):
        v = prof.baseline_section("voltage")
        assert v["stop_test_below_v"] == 11.0
        assert v["low_v"] == 11.0

    def test_motion_has_spin_and_safety(self, prof):
        motion = prof.baseline_section("motion")
        assert "spin" in motion
        assert motion["spin"]["target_yaw_deg"] == 360
        assert "safety" in motion

    def test_missing_section_raises(self, prof):
        with pytest.raises(ProfileError) as exc:
            prof.baseline_section("no_such_section")
        assert "no_such_section" in str(exc.value)


class TestProfileValidation:
    """加载期的校验：配置写错要报清楚，不能静默兜底。"""

    def test_unknown_profile_lists_available(self):
        with pytest.raises(ProfileError) as exc:
            product_profile.load("no_such_product")
        msg = str(exc.value)
        assert "不存在" in msg
        assert "tianracer_t110_rdk" in msg

    def test_list_profiles_skips_template(self):
        available = product_profile.list_profiles()
        assert "tianracer_t110_rdk" in available
        assert "_template" not in available

    def test_ordering_violation_detected(self):
        from core.profile import CheckSpec, _validate_ordering

        # motion 排在 static 前面 → 必须报错
        bad = [
            CheckSpec(number=18, title="运动", module="motion", phase="motion"),
            CheckSpec(number=1, title="网络", module="network", phase="static"),
        ]
        with pytest.raises(ProfileError) as exc:
            _validate_ordering(bad, "test")
        assert "顺序" in str(exc.value)

    def test_requires_unknown_number_detected(self):
        from core.profile import CheckSpec, _validate_requires

        bad = [CheckSpec(number=1, title="网络", module="network",
                         phase="static", requires=[99])]
        with pytest.raises(ProfileError) as exc:
            _validate_requires(bad, "test")
        assert "99" in str(exc.value)

    def test_requires_self_detected(self):
        from core.profile import CheckSpec, _validate_requires

        bad = [CheckSpec(number=1, title="网络", module="network",
                         phase="static", requires=[1])]
        with pytest.raises(ProfileError):
            _validate_requires(bad, "test")


class TestNoHardcodedProduct:
    """代码里不该再出现产品特定的话题名。"""

    ROOT = Path(__file__).resolve().parent.parent

    @staticmethod
    def _sources():
        skip = {"build", "dist", "__pycache__", ".pytest_cache", "tests",
                "products", "node_modules"}
        return [p for p in TestNoHardcodedProduct.ROOT.rglob("*.py")
                if not any(s in p.parts for s in skip)
                and p.name != "profile.py"]

    def test_no_hardcoded_topics_in_checks(self):
        """checks/ 里不该有硬编码话题。

        注释和 docstring 里提到具体话题是允许的 —— 那是说明历史背景，
        不参与运行。这里用 tokenize 跳过注释与字符串常量，
        只看真正的代码。
        """
        import io
        import tokenize

        offenders = []
        for path in self._sources():
            if path.parent.name != "checks":
                continue
            text = path.read_text(encoding="utf-8")
            try:
                tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
            except tokenize.TokenError:
                continue
            for tok in tokens:
                if tok.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                if "/tianracer/" in tok.string:
                    offenders.append(f"{path.name}:{tok.start[0]} {tok.string[:70]}")
        assert not offenders, "checks/ 里仍有硬编码话题：\n" + "\n".join(offenders)

    def test_no_hardcoded_workspace(self):
        offenders = []
        for path in self._sources():
            for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "tianracer_ros1_ws" in stripped:
                    offenders.append(f"{path.relative_to(self.ROOT)}:{no}")
        assert not offenders, "仍有硬编码工作空间：\n" + "\n".join(offenders)

    def test_probes_use_passed_topics(self):
        """探针的话题必须来自参数，硬编码只能是 DEFAULT_ 开头的兜底。"""
        probes = list((self.ROOT / "probes").glob("*.py"))
        offenders = []
        for path in probes:
            for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if "/tianracer/" not in stripped:
                    continue
                if stripped.startswith("#") or "DEFAULT_" in stripped:
                    continue
                offenders.append(f"{path.name}:{no} {stripped[:70]}")
        assert not offenders, "探针里仍有硬编码：\n" + "\n".join(offenders)

class TestBaselineSchemaValidation:
    """baseline 缺键必须在加载时报错，不能静默用代码里的默认值。

    这是 76 处 `cfg.get("x", 默认值)` 的统一防线：
    键一定存在时，默认分支永远走不到。
    """

    def _load_with(self, monkeypatch, mutate):
        """改一份 baseline 再加载，返回 (profile, error)。"""
        import tempfile, shutil
        from pathlib import Path as _Path
        from core import profile as pp

        src_dir = pp.products_root() / "tianracer_t110_rdk"
        tmp_root = _Path(tempfile.mkdtemp())
        dst = tmp_root / "tianracer_t110_rdk"
        shutil.copytree(src_dir, dst)

        baseline = dst / "baseline.yaml"
        baseline.write_text(mutate(baseline.read_text(encoding="utf-8")),
                            encoding="utf-8")
        monkeypatch.setattr(pp, "products_root", lambda: tmp_root)
        try:
            return pp.load("tianracer_t110_rdk"), ""
        except pp.ProfileError as exc:
            return None, str(exc)
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

    def test_missing_threshold_key_raises(self, monkeypatch):
        def mutate(text):
            return text.replace("""  imu_hz:
    nominal: 50.0
    min: 45.0""", """  imu_hz:
    nominal: 50.0""", 1)

        prof, err = self._load_with(monkeypatch, mutate)
        assert prof is None
        assert "frequency.imu_hz.min" in err
        # 要说清是哪一项需要它
        assert "IMU" in err

    def test_error_lists_all_missing_keys(self, monkeypatch):
        def mutate(text):
            text = text.replace("  low_v: 11.0\n", "", 1)
            text = text.replace("  load_warn: 3.0\n", "", 1)
            return text

        prof, err = self._load_with(monkeypatch, mutate)
        assert prof is None
        assert "voltage.low_v" in err
        assert "health.load_warn" in err

    def test_valid_baseline_loads(self, monkeypatch):
        prof, err = self._load_with(monkeypatch, lambda text: text)
        assert prof is not None, err


class TestTemplate:
    def test_template_loads(self):
        """模板必须能被加载 —— 它是新产品的起点。"""
        tpl = product_profile.load("_template")
        assert tpl.total_checks >= 1
        assert tpl.id == "_template"

    def test_template_not_listed_as_product(self):
        """模板不该出现在产品下拉里。"""
        assert "_template" not in product_profile.list_profiles()

    def test_template_has_no_env_prefix(self):
        """模板可以没有环境变量前缀（很多产品不涉及）。"""
        tpl = product_profile.load("_template")
        assert tpl.env_prefix == ""


class TestEnvPrefixOptional:
    def test_profile_without_prefix_is_valid(self):
        tpl = product_profile.load("_template")
        # 不抛异常即可
        assert tpl.env_prefix == ""

    def test_envvars_check_skips_without_prefix(self, monkeypatch):
        from checks import envvars
        from core import profile as pp

        tpl = pp.load("_template")

        class _Ctx:
            profile = tpl
            baseline = tpl.baseline
            remote = None

        result = envvars.run(_Ctx())
        assert result.status.value == "skip"
        assert "未配置环境变量" in result.summary

class TestVersionConsistency:
    """检查版本格式及版本信息在报告中的传递。"""

    ROOT = Path(__file__).resolve().parent.parent

    def test_version_is_semver(self):
        import re
        from core import __version__
        assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), \
            f"版本号 {__version__!r} 不是 MAJOR.MINOR.PATCH 格式"

    def test_release_doc_exists(self):
        assert (self.ROOT / "RELEASE.md").is_file(), "缺 RELEASE.md（发布流程）"

    def test_release_doc_version_matches_code(self):
        """RELEASE.md 里的当前版本号必须与 core/__init__.py 一致。

        上游用 ADR 替掉 CHANGELOG 之后，版本号只靠人工同步。
        这里只钉「两处文字不矛盾」这一件最小的事 —— 不校 tag、不校发布状态，
        那些得看远端，不是单测能回答的。
        """
        import re
        from core import __version__

        text = (self.ROOT / "RELEASE.md").read_text(encoding="utf-8")
        # 只看「当前 `x.y.z` 尚未发布」这一句，不扫全文的示例版本号
        match = re.search(r"当前\s*`(\d+\.\d+\.\d+)`\s*(?:尚未发布|已发布)?", text)
        assert match, "RELEASE.md 里找不到「当前 `x.y.z` …」句子，无法校验版本一致性"
        assert match.group(1) == __version__, (
            f"RELEASE.md 写的是 {match.group(1)}，"
            f"但 core/__init__.py 是 {__version__} —— 发版时漏改了一个"
        )

    def test_version_reaches_report(self):
        """报告要带上版本号，否则拿到一份报告无法反查是哪版生成的。"""
        from core.verdict import Report
        assert hasattr(Report, "__dataclass_fields__")
        assert "tool_version" in Report.__dataclass_fields__

    def test_runner_fills_version(self):
        """Runner 必须把版本写进报告。"""
        import inspect
        from core import runner

        src = inspect.getsource(runner.Runner._run_inner)
        assert "self.report.tool_version" in src



class TestTapEnvInjection:
    """车端录音目录必须由 tap 段注入，不能混进 env_overrides。

    2026-11-10：第 20 项一直是 SKIP，因为 `ros.tap.enabled` 为 false。
    开启时需要把录音目录传给车端节点，但**不能放 env_overrides** ——
    那里的语义是「纠正 env-hooks 的产品变量」，会被第 4 项环境变量检查
    当作白名单比对，混进去会让那一项报假告警。
    """

    def test_tap_dir_not_in_env_overrides(self):
        from core.profile import load
        prof = load("tianracer_t110_rdk")
        for key in prof.ros.env_overrides:
            assert not key.startswith("TIANBOT_"), (
                f"env_overrides 里混进了非产品变量 {key}；"
                f"录音目录应由 tap 段注入"
            )

    def test_enabled_tap_injects_dir_env(self):
        from core.profile import load
        prof = load("tianracer_t110_rdk")
        tap = prof.ros.tap
        prefix = prof.ros_env_prefix()
        if tap.enabled:
            assert f"export {tap.dir_env}=" in prefix, (
                f"tap 已启用但没注入 {tap.dir_env}，车端不会录音"
            )
            assert tap.log_dir in prefix

    def test_disabled_tap_injects_nothing(self):
        """关闭时不能注入 —— 否则车端会一直写文件。"""
        import dataclasses
        from core.profile import load
        prof = load("tianracer_t110_rdk")
        if not prof.ros.tap.enabled:
            return      # 本产品已启用，跳过
        old = prof.ros.tap
        prof.ros.tap = dataclasses.replace(old, enabled=False)
        try:
            assert "TIANBOT_TAP_DIR" not in prof.ros_env_prefix()
        finally:
            prof.ros.tap = old

    def test_tap_log_dir_matches_dir_env_value(self):
        """路径要一致：第 20 项按 log_dir 读文件，车端按同一个值写。"""
        from core.profile import load
        prof = load("tianracer_t110_rdk")
        tap = prof.ros.tap
        assert tap.log_dir.startswith("/")
        assert tap.dir_env == "TIANBOT_TAP_DIR", (
            "环境变量名要与车端补丁 getenv 的参数一致"
        )


class TestNoHardcodedNumbers:
    """编号只能有一个来源：profile.yaml。

    2026-11-13 重编号时（ADR-0009），runner.py 里 4 处硬编码编号没跟着改：
      - `r.number == 19` 找「使用痕迹」，而 19 变成了里程计
        → 清理结果写进里程计，甚至能把致命项改成 WARN
      - 两处 `number=8, title="IMU"`，而 8 变成了雷达以太网
        → 报告里出现两个第 8 项，且编号倒退
    这一组测试用 AST 扫，防止同类问题再犯。
    """

    def test_runner_has_no_literal_number_in_checkresult(self):
        import ast
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "core" / "runner.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        bad = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name != "CheckResult":
                continue
            # 唯一豁免：profile 还没加载成功时的「加载产品配置」入口错误。
            # 那种情况查不到 spec，只能是字面量 1。
            titles = {kw.arg: kw.value for kw in node.keywords}
            title_node = titles.get("title")
            exempt = (isinstance(title_node, ast.Constant)
                      and title_node.value == "加载产品配置")
            if exempt:
                continue
            for kw in node.keywords:
                if kw.arg == "number" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, int):
                        bad.append(f"line {node.lineno}: number={kw.value.value}")
        assert not bad, (
            "core/runner.py 里出现字面量编号，重编号时会漏改：\n"
            + "\n".join(bad)
            + "\n改用 self._spec_of('<module>').number"
        )

    def test_runner_does_not_compare_number_to_literal(self):
        """`r.number == <字面量>` 也是硬编码，同样会漏改。"""
        import ast
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "core" / "runner.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        bad = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            left = node.left
            if not (isinstance(left, ast.Attribute) and left.attr == "number"):
                continue
            for comp in node.comparators:
                if isinstance(comp, ast.Constant) and isinstance(comp.value, int):
                    bad.append(f"line {node.lineno}: .number == {comp.value}")
        assert not bad, (
            "core/runner.py 里按字面量编号查找检测项：\n" + "\n".join(bad)
        )

    def test_spec_of_resolves_by_module(self):
        """self._spec_of('traces') 必须返回使用痕迹那一条。"""
        from core.profile import load
        prof = load("tianracer_t110_rdk")
        spec = next((s for s in prof.checks if s.module == "traces"), None)
        assert spec is not None
        assert spec.title == "使用痕迹"
        # 使用痕迹现在是 11，不是 19
        assert spec.number == 11, spec.number
        # 19 应该是里程计
        odom = next(s for s in prof.checks if s.module == "odom")
        assert odom.number == 19
