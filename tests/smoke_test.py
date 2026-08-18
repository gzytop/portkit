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
import theme  # noqa: E402


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


class NonUtf8ConsoleTests(unittest.TestCase):
    """回归测试：中文输出不能在非 UTF-8 控制台上把脚本搞崩。

    英文版 Windows 与 GitHub Actions 的 Windows runner 默认是 cp1252，
    曾经导致 `make_icon.py` 直接抛 UnicodeEncodeError，让构建失败在生成图标这步。
    这里通过 PYTHONIOENCODING 复现该环境。
    """

    def _run_with_console_encoding(self, *arguments: str) -> subprocess.CompletedProcess:
        constrained_environment = dict(os.environ)
        constrained_environment["PYTHONIOENCODING"] = "cp1252"
        return subprocess.run(
            [sys.executable, *arguments],
            capture_output=True,
            timeout=120,
            cwd=str(PROJECT_ROOT),
            env=constrained_environment,
        )

    def test_icon_generation_survives_cp1252_console(self):
        completed = self._run_with_console_encoding("make_icon.py")
        self.assertEqual(
            completed.returncode,
            0,
            f"make_icon.py 在 cp1252 控制台下失败：{completed.stderr.decode('utf-8', 'replace')}",
        )

    def test_cli_survives_cp1252_console(self):
        free_port = reserve_free_port()
        completed = self._run_with_console_encoding("portkit.py", "check", str(free_port), "--no-color")
        self.assertEqual(
            completed.returncode,
            0,
            f"portkit.py 在 cp1252 控制台下失败：{completed.stderr.decode('utf-8', 'replace')}",
        )


class DesignTokenTests(unittest.TestCase):
    """色板必须满足 WCAG AA，且这条约束要被自动守住。

    颜色是这个工具传达「能不能杀」的主要手段，配色退化会直接损害可用性，
    所以对比度不能靠肉眼判断——两套主题的每个真实组合都在这里断言。
    """

    def test_oklch_conversion_produces_valid_hex(self):
        for lightness in (0.0, 0.25, 0.5, 0.75, 1.0):
            hex_color = theme.oklch_to_hex(lightness, 0.1, 250.0)
            self.assertRegex(hex_color, r"^#[0-9a-f]{6}$")

    def test_oklch_lightness_is_monotonic(self):
        """亮度递增时，感知亮度也应递增——这是色板推导成立的前提。"""
        luminances = [
            theme.relative_luminance(theme.oklch_to_hex(lightness, 0.05, 250.0))
            for lightness in (0.2, 0.4, 0.6, 0.8)
        ]
        self.assertEqual(luminances, sorted(luminances))

    def test_contrast_ratio_matches_known_values(self):
        # 黑白对比度是 21:1，同色是 1:1，用这两个已知值验证公式实现。
        self.assertAlmostEqual(theme.contrast_ratio("#000000", "#ffffff"), 21.0, places=1)
        self.assertAlmostEqual(theme.contrast_ratio("#777777", "#777777"), 1.0, places=2)

    def test_light_palette_meets_wcag_aa(self):
        self._assert_palette_meets_aa(theme.LIGHT_PALETTE)

    def test_dark_palette_meets_wcag_aa(self):
        self._assert_palette_meets_aa(theme.DARK_PALETTE)

    def _assert_palette_meets_aa(self, palette: theme.Palette) -> None:
        for label, foreground, background in theme.critical_contrast_pairs(palette):
            ratio = theme.contrast_ratio(foreground, background)
            self.assertGreaterEqual(
                ratio,
                4.5,
                f"[{palette.name}] {label} 对比度仅 {ratio:.2f}:1，低于 WCAG AA 要求的 4.5:1",
            )

    def test_neutrals_are_tinted_toward_brand_hue(self):
        """中性色应带一点品牌色相，纯灰会让界面显得廉价。"""
        surface_digits = theme.LIGHT_PALETTE.surface.lstrip("#")
        red, green, blue = (int(surface_digits[i : i + 2], 16) for i in (0, 2, 4))
        self.assertGreaterEqual(blue, red, "亮色表面应偏冷（蓝通道不低于红通道）")

    def test_themes_are_actually_different(self):
        self.assertNotEqual(theme.LIGHT_PALETTE.window, theme.DARK_PALETTE.window)
        self.assertFalse(theme.LIGHT_PALETTE.is_dark)
        self.assertTrue(theme.DARK_PALETTE.is_dark)

    def test_checkbox_mark_is_legible_in_both_themes(self):
        """回归测试：暗色下曾经看不出复选框有没有勾。

        原因是方框底色用了 text_secondary（亮灰），而勾选标记的颜色跟随
        foreground（近白），两者只有 2:1 —— 三个复选框看起来完全一样，
        用户无法判断「隐藏系统进程」到底开着没有。
        """
        for palette in (theme.LIGHT_PALETTE, theme.DARK_PALETTE):
            with self.subTest(theme=palette.name):
                mark_contrast = theme.contrast_ratio(palette.text_primary, palette.control_field)
                self.assertGreaterEqual(
                    mark_contrast,
                    4.5,
                    f"[{palette.name}] 勾选标记与方框底只有 {mark_contrast:.2f}:1，勾没勾看不出来",
                )

    def test_table_header_stands_apart_from_data_rows(self):
        """表头要自成一层，否则整张表看起来只是一堆同色的行。

        用对比度比值而不是亮度差来断言：暗色区间的相对亮度绝对值本身很小，
        同样的感知差异算出来的亮度差会比亮色小一个量级，用绝对差会误判。
        """
        for palette in (theme.LIGHT_PALETTE, theme.DARK_PALETTE):
            with self.subTest(theme=palette.name):
                header_ratio = theme.contrast_ratio(palette.table_header, palette.surface)
                self.assertGreater(
                    header_ratio,
                    1.15,
                    f"[{palette.name}] 表头与数据区只有 {header_ratio:.3f}:1，分不出层级",
                )

    def test_row_stripe_is_distinguishable_from_surface(self):
        """斑马纹的唯一职责是让人看清行边界，太淡就等于没有。"""
        for palette in (theme.LIGHT_PALETTE, theme.DARK_PALETTE):
            with self.subTest(theme=palette.name):
                stripe_contrast = theme.contrast_ratio(palette.row_stripe, palette.surface)
                self.assertGreater(
                    stripe_contrast,
                    1.05,
                    f"[{palette.name}] 斑马纹与表面几乎同色（{stripe_contrast:.3f}:1），起不到分隔作用",
                )

    def test_dark_theme_is_darker_than_light(self):
        light_luminance = theme.relative_luminance(theme.LIGHT_PALETTE.window)
        dark_luminance = theme.relative_luminance(theme.DARK_PALETTE.window)
        self.assertGreater(light_luminance, dark_luminance)


class DispositionClassificationTests(unittest.TestCase):
    """处置分类决定表格文案与能否终止，必须与 portkit 的保护策略一致。"""

    @classmethod
    def setUpClass(cls):
        import portkit_gui

        cls.portkit_gui = portkit_gui

    def _make_binding(self, pid: int, process_name: str) -> portkit.PortBinding:
        return portkit.PortBinding("TCP", "0.0.0.0", 3000, "LISTENING", pid, process_name)

    def test_kernel_placeholder_is_classified_as_kernel(self):
        self.assertEqual(
            self.portkit_gui.classify_binding(self._make_binding(0, "System Idle Process")),
            self.portkit_gui.DISPOSITION_KERNEL,
        )

    def test_system_process_is_classified_as_protected(self):
        self.assertEqual(
            self.portkit_gui.classify_binding(self._make_binding(1234, "svchost.exe")),
            self.portkit_gui.DISPOSITION_PROTECTED,
        )

    def test_ordinary_process_is_classified_as_killable(self):
        self.assertEqual(
            self.portkit_gui.classify_binding(self._make_binding(1234, "node.exe")),
            self.portkit_gui.DISPOSITION_KILLABLE,
        )

    def test_every_disposition_has_a_readable_label(self):
        """状态不能只靠颜色区分，每种分类都要有文字标签。"""
        for disposition in (
            self.portkit_gui.DISPOSITION_KILLABLE,
            self.portkit_gui.DISPOSITION_PROTECTED,
            self.portkit_gui.DISPOSITION_KERNEL,
        ):
            self.assertIn(disposition, self.portkit_gui.DISPOSITION_LABELS)
            self.assertTrue(self.portkit_gui.DISPOSITION_LABELS[disposition].strip())

    def test_classification_agrees_with_protection_policy(self):
        """分类为「可终止」的记录，不能是 portkit 认定的受保护进程。"""
        for process_name in ("System", "lsass.exe", "svchost.exe", "systemd"):
            binding = self._make_binding(4321, process_name)
            self.assertNotEqual(
                self.portkit_gui.classify_binding(binding),
                self.portkit_gui.DISPOSITION_KILLABLE,
                f"{process_name} 受 portkit 保护，却被分类为可终止",
            )


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


@unittest.skipUnless(graphical_display_is_available(), "当前环境没有可用的图形显示")
class LayoutFitTests(unittest.TestCase):
    """回归测试：控件不能因为空间不足而被挤掉文字。

    起因是一个真实缺陷：筛选栏里塞了搜索、协议、三个复选框、自动刷新和主题按钮，
    在 DPI 感知开启（打包成 exe 后的实际情况）时中文字体渲染变宽，
    所需宽度超过窗口，`pack` 便压缩了最后放入的元素，
    「自动刷新」四个字直接消失，只剩一个孤立的勾选框。

    这类问题在开发机上不一定复现（取决于 DPI 缩放与字体），
    所以这里显式检查每个控件拿到的宽度不小于它请求的宽度。
    """

    @classmethod
    def setUpClass(cls):
        import portkit_gui

        cls.portkit_gui = portkit_gui

    def _find_squeezed_widgets(self, container) -> list[str]:
        """递归找出实际宽度小于请求宽度的控件。"""
        squeezed: list[str] = []
        for child in container.winfo_children():
            requested = child.winfo_reqwidth()
            actual = child.winfo_width()
            # 容差 1px 吸收取整误差；宽度为 1 说明还没被布局，跳过。
            if actual > 1 and requested - actual > 1:
                label = ""
                try:
                    label = str(child.cget("text"))
                except Exception:
                    pass
                squeezed.append(
                    f"{child.winfo_class()}(req={requested}, actual={actual}, text={label!r})"
                )
            squeezed.extend(self._find_squeezed_widgets(child))
        return squeezed

    def _assert_layout_fits_at_width(self, width: int, height: int = 620) -> None:
        import tkinter as tk

        # 与打包后的 exe 保持一致：先开 DPI 感知，中文字体才会按真实尺寸渲染。
        self.portkit_gui.enable_high_dpi_awareness()

        root = tk.Tk()
        application = self.portkit_gui.PortManagerApplication(root)
        try:
            root.geometry(f"{width}x{height}")
            # 多轮 update 让 pack 完成尺寸协商。
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                root.update()
                time.sleep(0.03)

            squeezed = self._find_squeezed_widgets(root)
            self.assertEqual(
                squeezed,
                [],
                f"窗口宽 {width}px 时下列控件被挤压，文字会显示不全：\n  "
                + "\n  ".join(squeezed),
            )
        finally:
            application.handle_window_close()

    def test_layout_fits_at_minimum_window_width(self):
        """最小窗口尺寸是用户能拖到的极限，这里必须不挤。"""
        self._assert_layout_fits_at_width(940)

    def test_layout_fits_at_default_window_width(self):
        self._assert_layout_fits_at_width(1080)


class ReleaseNotesTests(unittest.TestCase):
    """Release 说明必须逐版本不同。

    此前模板完全静态，v1.0.0 到 v1.2.1 的说明除 SHA256 外一模一样，
    用户无法从 Release 页面看出改了什么。这里守住修复后的行为。
    """

    @classmethod
    def setUpClass(cls):
        # 脚本位于 .github/scripts 下，包名不合法，只能按路径加载。
        import importlib.util

        script_path = PROJECT_ROOT / ".github" / "scripts" / "render_release_notes.py"
        spec = importlib.util.spec_from_file_location("render_release_notes", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.renderer = module

    def test_extracts_only_the_requested_version_section(self):
        changelog_text = (
            "# 更新日志\n\n"
            "## 未发布\n\n（占位）\n\n"
            "## v2.0.0 — 2026-09-01\n\n- 新版本的条目\n\n"
            "## v1.9.0 — 2026-08-01\n\n- 旧版本的条目\n"
        )
        section = self.renderer.extract_changelog_section(changelog_text, "v1.9.0")
        self.assertIn("旧版本的条目", section)
        self.assertNotIn("新版本的条目", section)
        self.assertNotIn("占位", section)
        # 标题行本身不应混进正文，否则 Release 里会出现重复的版本号标题。
        self.assertNotIn("## v1.9.0", section)

    def test_missing_version_section_aborts_the_release(self):
        """漏写小节要让发版失败，而不是退回通用文案。"""
        changelog_text = "# 更新日志\n\n## v1.0.0 — 2026-08-16\n\n- 首个版本\n"
        with self.assertRaises(SystemExit):
            self.renderer.extract_changelog_section(changelog_text, "v9.9.9")

    def test_empty_version_section_aborts_the_release(self):
        changelog_text = "# 更新日志\n\n## v1.0.0 — 2026-08-16\n\n## v0.9.0 — 2026-08-01\n\n- 旧的\n"
        with self.assertRaises(SystemExit):
            self.renderer.extract_changelog_section(changelog_text, "v1.0.0")

    def test_accepts_full_ref_and_bare_tag(self):
        self.assertEqual(self.renderer.normalize_version_tag("refs/tags/v1.2.3"), "v1.2.3")
        self.assertEqual(self.renderer.normalize_version_tag("  v1.2.3\n"), "v1.2.3")

    def test_every_released_tag_has_a_changelog_section(self):
        """已发布的版本都得能渲染出说明，避免 CHANGELOG 与 tag 脱节。"""
        changelog_text = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        for version_tag in ("v1.0.0", "v1.1.0", "v1.2.0", "v1.2.1"):
            with self.subTest(version=version_tag):
                section = self.renderer.extract_changelog_section(changelog_text, version_tag)
                self.assertTrue(section.strip(), f"{version_tag} 的小节为空")

    def test_rendered_notes_differ_between_versions(self):
        rendered_previous = self.renderer.render_release_notes(
            sha256="A" * 64, repository="gzytop/portkit", version_tag="v1.2.0"
        )
        rendered_current = self.renderer.render_release_notes(
            sha256="A" * 64, repository="gzytop/portkit", version_tag="v1.2.1"
        )
        self.assertNotEqual(
            rendered_previous,
            rendered_current,
            "两个版本的 Release 说明内容相同，说明「本次更新」没有真的注入",
        )
        self.assertNotIn("{{", rendered_current, "仍有未替换的占位符")


if __name__ == "__main__":
    unittest.main(verbosity=2)
