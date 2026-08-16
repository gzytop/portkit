#!/usr/bin/env python3
"""portkit 冒烟测试：验证核心逻辑在当前平台上真实可用。

只依赖标准库 unittest，本地和 CI 共用同一份测试：

    python tests/smoke_test.py           # 全部测试
    python tests/smoke_test.py -v        # 显示每项名称
    python -m unittest discover tests    # 也可以用 unittest 发现

测试策略：
  * 纯函数用固定输入验证，平台无关；
  * 端口相关的测试真实占用一个由系统分配的空闲端口，
    确保「能发现占用」「能终止进程」在当前平台的实现路径上真的成立，
    而不是只测了 Windows 分支；
  * GUI 测试在没有图形显示的环境下自动跳过。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import portkit  # noqa: E402  (必须在调整 sys.path 之后导入)


def reserve_free_port() -> int:
    """让操作系统分配一个当前空闲的端口，避免测试之间抢占固定端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def find_bindings_for_port(port: int) -> list[portkit.PortBinding]:
    return portkit.filter_bindings(
        portkit.collect_port_bindings(), ports=[port], listening_only=False
    )


def wait_for_condition(predicate, timeout_seconds: float = 15.0, interval: float = 0.3) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class AddressParsingTests(unittest.TestCase):
    """`split_address_and_port` 需要覆盖各平台 netstat/lsof/ss 的地址写法。"""

    def test_parses_ipv4_endpoint(self):
        self.assertEqual(portkit.split_address_and_port("0.0.0.0:8080"), ("0.0.0.0", 8080))

    def test_parses_ipv6_endpoint(self):
        self.assertEqual(portkit.split_address_and_port("[::]:8080"), ("[::]", 8080))

    def test_parses_wildcard_endpoint(self):
        self.assertEqual(portkit.split_address_and_port("*:5353"), ("*", 5353))

    def test_returns_none_port_when_not_numeric(self):
        _, port = portkit.split_address_and_port("localhost:http")
        self.assertIsNone(port)

    def test_returns_none_port_when_no_separator(self):
        _, port = portkit.split_address_and_port("0.0.0.0")
        self.assertIsNone(port)


class ProtectedProcessTests(unittest.TestCase):
    """系统关键进程与内核占位必须被识别为受保护，否则误杀会拖垮系统。"""

    def _make_binding(self, pid: int, process_name: str) -> portkit.PortBinding:
        return portkit.PortBinding(
            protocol="TCP",
            local_address="0.0.0.0",
            local_port=445,
            state="LISTENING",
            pid=pid,
            process_name=process_name,
        )

    def test_kernel_placeholder_pids_are_protected(self):
        for kernel_pid in sorted(portkit.PROTECTED_PIDS):
            self.assertTrue(self._make_binding(kernel_pid, "whatever").is_protected)

    def test_critical_process_names_are_protected(self):
        for critical_name in ("System", "lsass.exe", "svchost.exe", "systemd", "launchd"):
            self.assertTrue(
                self._make_binding(4242, critical_name).is_protected,
                f"{critical_name} 应被识别为系统关键进程",
            )

    def test_ordinary_process_is_not_protected(self):
        self.assertFalse(self._make_binding(4242, "node.exe").is_protected)


class BindingFilterTests(unittest.TestCase):
    def setUp(self):
        self.sample_bindings = [
            portkit.PortBinding("TCP", "0.0.0.0", 3000, "LISTENING", 100, "node"),
            portkit.PortBinding("TCP", "[::]", 3000, "LISTENING", 100, "node"),
            portkit.PortBinding("UDP", "0.0.0.0", 5353, "", 200, "mdns"),
            portkit.PortBinding("TCP", "127.0.0.1", 8080, "ESTABLISHED", 300, "java"),
        ]

    def test_deduplicates_same_pid_on_ipv4_and_ipv6(self):
        selected = portkit.filter_bindings(self.sample_bindings, ports=[3000])
        self.assertEqual(len(selected), 1, "同一 PID 在双栈上监听同端口应合并为一条")

    def test_listening_only_excludes_established(self):
        selected = portkit.filter_bindings(self.sample_bindings, listening_only=True)
        self.assertNotIn(8080, [item.local_port for item in selected])

    def test_including_all_states_keeps_established(self):
        selected = portkit.filter_bindings(self.sample_bindings, listening_only=False)
        self.assertIn(8080, [item.local_port for item in selected])

    def test_protocol_filter_is_case_insensitive(self):
        selected = portkit.filter_bindings(self.sample_bindings, protocol="udp", listening_only=False)
        self.assertEqual({item.protocol for item in selected}, {"UDP"})

    def test_name_keyword_filter(self):
        selected = portkit.filter_bindings(
            self.sample_bindings, name_keyword="NODE", listening_only=False
        )
        self.assertTrue(selected)
        self.assertTrue(all("node" in item.process_name.lower() for item in selected))


class TableRenderingTests(unittest.TestCase):
    def test_east_asian_characters_count_as_double_width(self):
        self.assertEqual(portkit.display_width("端口"), 4)
        self.assertEqual(portkit.display_width("port"), 4)

    def test_padding_aligns_mixed_width_text(self):
        padded = portkit.pad_to_width("端口", 10)
        self.assertEqual(portkit.display_width(padded), 10)

    def test_empty_table_reports_no_rows(self):
        rendered = portkit.render_bindings_table([], portkit.Palette(False))
        self.assertIn("无匹配记录", rendered)

    def test_kernel_placeholder_is_labelled_instead_of_process_name(self):
        kernel_binding = portkit.PortBinding("TCP", "127.0.0.1", 7890, "TIME_WAIT", 0, "System Idle Process")
        rendered = portkit.render_bindings_table([kernel_binding], portkit.Palette(False))
        self.assertIn("内核残留连接", rendered)
        self.assertNotIn("System Idle Process", rendered)


class PortDiscoveryTests(unittest.TestCase):
    """真实占用一个端口，验证当前平台的采集实现确实能发现它。

    这组测试在 Windows 上走 netstat 分支，在 macOS/Linux 上走 lsof/ss 分支。
    """

    def test_collect_returns_bindings(self):
        bindings = portkit.collect_port_bindings()
        self.assertTrue(bindings, "应至少采集到一条端口记录")
        self.assertTrue(all(isinstance(item, portkit.PortBinding) for item in bindings))

    def test_discovers_a_port_this_process_is_listening_on(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listening_port = listener.getsockname()[1]

            found = wait_for_condition(lambda: bool(find_bindings_for_port(listening_port)))
            self.assertTrue(found, f"未能发现本进程正在监听的端口 {listening_port}")

            matched = find_bindings_for_port(listening_port)
            self.assertIn(
                os.getpid(),
                [item.pid for item in matched],
                "采集结果里应包含当前进程的 PID",
            )

    def test_reports_free_port_as_unoccupied(self):
        free_port = reserve_free_port()
        self.assertEqual(find_bindings_for_port(free_port), [])


class ProcessTerminationTests(unittest.TestCase):
    """起一个真实的子进程占用端口，再用 portkit 终止它。"""

    def setUp(self):
        self.server_port = reserve_free_port()
        # http.server 是标准库自带的，任何平台都能起，适合当被终止的靶子。
        self.server_process = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(self.server_port), "--bind", "127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        started = wait_for_condition(lambda: bool(find_bindings_for_port(self.server_port)))
        if not started:
            self.server_process.kill()
            self.skipTest(f"测试用服务未能在端口 {self.server_port} 上启动")

    def tearDown(self):
        if self.server_process.poll() is None:
            self.server_process.kill()
            self.server_process.wait(timeout=10)

    def test_terminates_process_and_releases_port(self):
        occupying_pids = {item.pid for item in find_bindings_for_port(self.server_port) if item.pid > 0}
        self.assertIn(self.server_process.pid, occupying_pids)

        succeeded, detail = portkit.terminate_process(self.server_process.pid)
        self.assertTrue(succeeded, f"终止失败：{detail}")

        self.assertTrue(
            wait_for_condition(lambda: self.server_process.poll() is not None),
            "进程在终止后仍然存活",
        )
        self.assertTrue(
            portkit.wait_until_port_released(self.server_port, timeout_seconds=10.0),
            "端口在进程退出后仍显示被占用",
        )

    def test_terminating_own_child_is_not_reported_as_still_running(self):
        """回归测试：POSIX 上进程被杀后会先变成僵尸（已退出但未被父进程回收）。

        僵尸对 `os.kill(pid, 0)` 依然响应成功，若把它当作存活，终止操作会
        等到超时并误报「进程仍在运行」，让用户以为没杀掉。

        这里刻意不调用 `Popen.poll()` —— 它会顺带回收僵尸，从而掩盖问题。
        """
        succeeded, detail = portkit.terminate_process(self.server_process.pid, force=True)
        self.assertTrue(succeeded, f"终止自己的子进程时被误报为失败：{detail}")
        self.assertFalse(
            portkit.is_process_running(self.server_process.pid),
            "进程已退出，但仍被判定为运行中（僵尸状态未被正确识别）",
        )

    def test_is_process_running_reflects_reality(self):
        self.assertTrue(portkit.is_process_running(self.server_process.pid))
        portkit.terminate_process(self.server_process.pid, force=True)
        wait_for_condition(lambda: self.server_process.poll() is not None)
        self.assertFalse(portkit.is_process_running(self.server_process.pid))


class CommandLineInterfaceTests(unittest.TestCase):
    """以子进程方式调用 CLI，验证退出码约定（脚本会依赖它做判断）。"""

    def _run_cli(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "portkit.py"), *arguments],
            capture_output=True,
            timeout=120,
            cwd=str(PROJECT_ROOT),
        )

    def test_help_exits_successfully(self):
        self.assertEqual(self._run_cli("--help").returncode, 0)

    def test_list_exits_successfully(self):
        self.assertEqual(self._run_cli("ls", "--no-color").returncode, 0)

    def test_json_output_is_parseable(self):
        import json

        completed = self._run_cli("ls", "--json", "--no-color")
        self.assertEqual(completed.returncode, 0)
        parsed = json.loads(completed.stdout.decode("utf-8"))
        self.assertIsInstance(parsed, list)

    def test_dev_scan_exits_successfully(self):
        self.assertEqual(self._run_cli("dev", "--no-color").returncode, 0)

    def test_check_returns_zero_for_free_port(self):
        free_port = reserve_free_port()
        self.assertEqual(self._run_cli("check", str(free_port), "--no-color").returncode, 0)

    def test_check_returns_one_for_occupied_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            occupied_port = listener.getsockname()[1]
            wait_for_condition(lambda: bool(find_bindings_for_port(occupied_port)))

            completed = self._run_cli("check", str(occupied_port), "--no-color")
            self.assertEqual(
                completed.returncode, 1, "端口被占用时应返回 1，脚本依赖这个约定做条件判断"
            )

    def test_rejects_invalid_port_argument(self):
        self.assertNotEqual(self._run_cli("check", "99999", "--no-color").returncode, 0)


def graphical_display_is_available() -> bool:
    """判断当前环境能否创建窗口（CI 的 Linux 需要 xvfb 才行）。"""
    try:
        import tkinter
    except ImportError:
        return False
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        return False
    try:
        probe_window = tkinter.Tk()
    except Exception:
        return False
    probe_window.destroy()
    return True


@unittest.skipUnless(graphical_display_is_available(), "当前环境没有可用的图形显示")
class GraphicalInterfaceTests(unittest.TestCase):
    """GUI 冒烟测试：能建起来、能加载数据、过滤与按钮状态联动正常。"""

    @classmethod
    def setUpClass(cls):
        import tkinter as tk

        import portkit_gui

        cls.tk = tk
        cls.portkit_gui = portkit_gui

    def setUp(self):
        self.root = self.tk.Tk()
        self.application = self.portkit_gui.PortManagerApplication(self.root)

    def tearDown(self):
        self.application.handle_window_close()

    def _pump_until(self, predicate, timeout_seconds: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    def _wait_for_initial_load(self):
        self.assertTrue(
            self._pump_until(lambda: bool(self.application.all_bindings)),
            "GUI 未能在超时前加载到端口数据",
        )

    def test_loads_data_and_table_matches_model(self):
        self._wait_for_initial_load()
        self.assertEqual(
            len(self.application.table.get_children()),
            len(self.application.displayed_bindings),
            "表格行数应与数据模型一致",
        )

    def test_hiding_system_processes_reduces_rows(self):
        self._wait_for_initial_load()
        self.application.hide_system_processes.set(True)
        self.application._render_table()
        hidden_count = len(self.application.displayed_bindings)

        self.application.hide_system_processes.set(False)
        self.application._render_table()
        self.assertGreaterEqual(len(self.application.displayed_bindings), hidden_count)

    def test_protocol_filter_narrows_to_tcp(self):
        self._wait_for_initial_load()
        self.application.protocol_filter.set("TCP")
        self.application._render_table()
        protocols = {item.protocol for item in self.application.displayed_bindings}
        self.assertTrue(protocols <= {"TCP"})

    def test_sorting_toggles_between_ascending_and_descending(self):
        self._wait_for_initial_load()
        self.application._toggle_sort_by_column("pid")
        ascending = [item.pid for item in self.application.displayed_bindings]
        self.application._toggle_sort_by_column("pid")
        descending = [item.pid for item in self.application.displayed_bindings]
        self.assertEqual(ascending, sorted(ascending))
        self.assertEqual(descending, sorted(descending, reverse=True))

    def test_terminate_button_requires_selection(self):
        self._wait_for_initial_load()
        self.application.table.selection_remove(self.application.table.selection())
        self.root.update()
        self.assertEqual(str(self.application.terminate_button.cget("state")), "disabled")

    def test_closing_cancels_scheduled_callbacks(self):
        self._wait_for_initial_load()
        self.application.handle_window_close()
        self.assertTrue(self.application.is_closing)
        self.assertIsNone(self.application.scheduled_poll_id)
        self.assertIsNone(self.application.scheduled_auto_refresh_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
