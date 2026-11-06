"""`probes/serial_tap.py` 的安全不变量 —— 静态契约测试。

为什么用静态检查而不是跑起来
----------------------------
这个脚本只在车端（Linux + 真实 pty）运行，Windows 开发机 import 不了
`pty` / `tty`。但它的失效模式非常具体，而且**每一条都曾经真的写错过**：

  · `os.write(master_fd)` 没设非阻塞 → 队列满时整个 select 循环卡死，
    而上位机的 `pgrep` 仍然 seeing 进程活着，卡死完全不可见
  · `slave_fd` 全程不关 → master 写永不返回 EIO，
    「底盘节点连没连上」的判断恒为假
  · 串口写失败只 emit 一行 → 循环继续，停车指令静默丢失

这些靠人记不住（Round 9 审查就是这么发现的）。所以钉成断言。

新增实现时如果确实要改这些结构，得连同这里的断言一起改 ——
这正是我们要的：改动必须是有意识的。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "probes" / "serial_tap.py"


@pytest.fixture(scope="module")
def text() -> str:
    return SOURCE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tree(text: str) -> ast.Module:
    return ast.parse(text)


def _method(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"serial_tap.py 里没有 {name}()")


def test_master_fd_is_set_non_blocking(tree: ast.Module):
    """master 必须非阻塞，否则 pty 队列满会让转发循环卡死。"""
    setup = _method(tree, "setup")
    calls = [n for n in ast.walk(setup)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "set_blocking"]
    assert calls, "setup() 里必须调用 os.set_blocking(self.master_fd, False)"
    targets = {ast.unparse(c.args[0]) for c in calls if c.args}
    assert any("master_fd" in t for t in targets), targets


def test_slave_fd_is_closed_after_ready(tree: ast.Module):
    """自己持有的 slave fd 必须交出去，否则 EIO 语义永远不生效。"""
    setup = _method(tree, "setup")
    closes = [n for n in ast.walk(setup)
              if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)
              and n.func.attr == "close"
              and n.args and "slave_fd" in ast.unparse(n.args[0])]
    assert closes, "setup() 里必须 os.close(self.slave_fd)"


def test_serial_write_failure_is_fatal(tree: ast.Module):
    """串口写失败必须抛出，不能只 emit 一条日志就继续。"""
    to_mcu = _method(tree, "_to_mcu")
    raises = [n for n in ast.walk(to_mcu) if isinstance(n, ast.Raise)]
    assert raises, "_to_mcu() 里 ser.write 失败必须 raise（不能只 emit）"
    assert any("TapFatal" in ast.unparse(r) for r in raises)


def test_from_mcu_never_writes_master_synchronously(tree: ast.Module):
    """_from_mcu 不能直接阻塞写 master —— 必须走 pending 队列。"""
    from_mcu = _method(tree, "_from_mcu")
    direct = [n for n in ast.walk(from_mcu)
              if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)
              and n.func.attr == "write"
              and n.args and "master_fd" in ast.unparse(n.args[0])]
    assert not direct, (
        "_from_mcu() 不能直接 os.write(master_fd)；"
        "写不下的部分要进 pending 队列，由 select 可写事件继续冲"
    )
    assert "pending_from_mcu" in ast.unparse(from_mcu)


def test_stdout_emit_is_non_blocking(text: str, tree: ast.Module):
    """上位机停止读取时，print 阻塞会把整个转发循环拖死。"""
    emit = _method(tree, "emit")
    assert "os.write" in ast.unparse(emit), "emit() 必须用 os.write 而非 print"
    assert "BlockingIOError" in ast.unparse(emit), "emit() 必须处理管道满"
    # main() 里要把 stdout 设成非阻塞
    main = _method(tree, "main")
    assert "set_blocking" in ast.unparse(main), "main() 里必须设 stdout 非阻塞"


def test_dropped_counter_names_the_right_direction(text: str):
    """`dropped_to_mcu` 曾统计 from_mcu 方向，排查时会看反。"""
    assert "dropped_from_mcu" in text
    assert "self.dropped_to_mcu" not in text


def test_safety_doc_does_not_promise_automatic_stop(text: str):
    """旧文档写「退出即安全」是错的：已发出的运动指令不会自动撤销。"""
    assert "退出即安全" not in text
    assert "架空" in text, "安全约定里必须写明采集期间车辆架空"


def test_pending_buffer_is_bounded(tree: ast.Module):
    """底盘节点不读 pty 时，积压不能无限涨。"""
    from_mcu = _method(tree, "_from_mcu")
    assert "MAX_PENDING_BYTES" in ast.unparse(from_mcu)


def test_loop_uses_poll_not_select(tree: ast.Module):
    """必须用 poll 而不是 select。

    select 报不了 POLLHUP。而「底盘节点开了 pty 但从不写数据」这种情况
    只能靠 HUP 发现 —— 靠「读到数据」永远发现不了。
    """
    loop = _method(tree, "loop")
    src = ast.unparse(loop)
    assert "select.poll" in src, "loop() 必须用 select.poll（select 报不了 HUP）"
    assert "POLLHUP" in src, "loop() 必须处理 POLLHUP"


def test_hup_does_not_clear_connected_flag_before_check(tree: ast.Module):
    """_on_pty_hup 不能先把 slave_connected 置 False 再判断。

    那样「连过又断开」会被当成「从未连过」，永远退不出去。
    """
    hup = _method(tree, "_on_pty_hup")
    body = ast.unparse(hup)
    raise_at = body.find("TapFatal")
    assert raise_at >= 0, "_on_pty_hup 必须在「连过又断开」时抛 TapFatal"
    assign_at = body.find("self.slave_connected = False")
    assert assign_at < 0 or assign_at > raise_at, (
        "slave_connected = False 不能出现在 raise 之前"
    )
