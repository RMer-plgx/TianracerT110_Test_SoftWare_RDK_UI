"""已配置产品的检测模块必须能被两个入口打包和界面自检发现。"""

import importlib.util

import app
import build
import build_cli
from core import profile


def test_each_product_is_complete_and_loadable():
    root = profile.products_root()
    product_dirs = [p for p in root.iterdir()
                    if p.is_dir() and not p.name.startswith(("_", "."))]
    assert product_dirs, "没有可验证的产品配置"
    for directory in product_dirs:
        assert (directory / "profile.yaml").is_file(), directory
        assert (directory / "baseline.yaml").is_file(), directory
        assert profile.load(directory.name).checks, directory


def test_configured_checks_are_importable_and_in_both_builds():
    for product_id in profile.list_profiles():
        modules = {spec.module for spec in profile.load(product_id).checks}
        for module in modules:
            assert importlib.util.find_spec(f"checks.{module}"), (product_id, module)
        for entry in (app, build, build_cli):
            assert modules <= set(entry.CHECK_MODULES), (product_id, entry.__name__,
                                                         modules - set(entry.CHECK_MODULES))
        for entry in (build, build_cli):
            assert {f"checks.{name}" for name in modules} <= set(entry.HIDDEN_IMPORTS), entry.__name__


def test_both_builds_include_shared_resources():
    for entry in (build, build_cli):
        assert {"products", "probes", "report"} <= set(entry.DATA_DIRS), entry.__name__
