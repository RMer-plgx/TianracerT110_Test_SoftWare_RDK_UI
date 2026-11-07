"""已配置产品的检测模块必须能被打包脚本和界面自检发现。"""

import importlib.util

import app
import build
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


def test_configured_checks_are_importable_and_in_all_entrypoints():
    for product_id in profile.list_profiles():
        modules = {spec.module for spec in profile.load(product_id).checks}
        for module in modules:
            assert importlib.util.find_spec(f"checks.{module}"), (product_id, module)
        for entry in (app, build):
            assert modules <= set(entry.CHECK_MODULES), (
                product_id, entry.__name__, modules - set(entry.CHECK_MODULES))
        assert {f"checks.{name}" for name in modules} <= set(build.HIDDEN_IMPORTS), \
            build.__name__


def test_build_includes_shared_resources():
    assert {"products", "probes", "report"} <= set(build.DATA_DIRS), build.__name__
