#!/usr/bin/env python3
"""portkit GUI - 端口占用可视化管理界面。

基于 tkinter（Python 标准库自带），复用 portkit.py 的采集与终止逻辑，
因此 GUI 与命令行的行为、安全策略完全一致。

启动:
  python portkit_gui.py
  或双击 port-gui.bat（无控制台窗口）
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import Callable, Sequence

from portkit import (
    COMMON_DEV_PORTS,
    IS_WINDOWS,
    SUBPROCESS_NO_WINDOW_FLAGS,
    PortBinding,
    PortToolError,
    collect_port_bindings,
    decode_console_bytes,
    filter_bindings,
    terminate_process,
    wait_until_port_released,
)

WINDOW_TITLE = "portkit — 端口占用管理"
PROTOCOL_FILTER_ALL = "全部"
PROTOCOL_FILTER_CHOICES = (PROTOCOL_FILTER_ALL, "TCP", "UDP")
AUTO_REFRESH_INTERVAL_CHOICES = {"2 秒": 2000, "5 秒": 5000, "10 秒": 10000, "30 秒": 30000}
DEFAULT_AUTO_REFRESH_LABEL = "5 秒"
BACKGROUND_POLL_INTERVAL_MS = 100

COLOR_WINDOW_BACKGROUND = "#f4f5f7"
COLOR_SURFACE = "#ffffff"
COLOR_TEXT_PRIMARY = "#1f2328"
COLOR_TEXT_MUTED = "#6b7280"
COLOR_ACCENT = "#2563eb"
COLOR_ACCENT_ACTIVE = "#1d4ed8"
COLOR_DANGER = "#dc2626"
COLOR_DANGER_ACTIVE = "#b91c1c"
COLOR_SYSTEM_ROW_BACKGROUND = "#fff7ed"
COLOR_SYSTEM_ROW_TEXT = "#b45309"
COLOR_KERNEL_ROW_TEXT = "#9ca3af"
COLOR_STRIPE_BACKGROUND = "#f1f3f5"

TABLE_COLUMNS = (
    ("port", "端口", 80, "center"),
    ("protocol", "协议", 70, "center"),
    ("state", "状态", 110, "center"),
    ("pid", "PID", 80, "center"),
    ("process", "进程", 240, "w"),
    ("address", "监听地址", 160, "w"),
)


@dataclass
class TerminationOutcome:
    """一次终止尝试的结果，用于回到主线程后汇总展示。"""

    pid: int
    process_name: str
    succeeded: bool
    detail: str


def enable_high_dpi_awareness() -> None:
    """让 Windows 高分屏下的界面保持清晰，而不是被系统拉伸变模糊。"""
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def resolve_bundled_resource(relative_name: str) -> Path:
    """定位随程序分发的资源文件。

    PyInstaller 单文件模式会把数据解压到 sys._MEIPASS 指向的临时目录，
    直接用 __file__ 的同级目录会找不到文件。
    """
    bundle_directory = getattr(sys, "_MEIPASS", None)
    base_directory = Path(bundle_directory) if bundle_directory else Path(__file__).parent
    return base_directory / relative_name


def apply_window_icon(root: tk.Tk) -> None:
    """尽力设置窗口图标；失败不应影响程序可用性。"""
    icon_path = resolve_bundled_resource("app_icon.ico")
    if not icon_path.exists():
        return
    try:
        root.iconbitmap(default=str(icon_path))
    except tk.TclError:
        pass


def pick_first_available_font(candidate_names: Sequence[str], fallback_name: str) -> str:
    installed_families = set(tkfont.families())
    for candidate in candidate_names:
        if candidate in installed_families:
            return candidate
    return fallback_name


def fetch_process_command_line(pid: int) -> str:
    """查询进程的完整命令行，用于判断「这个 node 到底是哪个项目」。"""
    if pid <= 0:
        return "（内核占位，无对应进程）"

    if IS_WINDOWS:
        powershell_script = (
            f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}')."
            "CommandLine"
        )
        command = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            powershell_script,
        ]
    else:
        command = ["ps", "-p", str(pid), "-o", "args="]

    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=8,
            creationflags=SUBPROCESS_NO_WINDOW_FLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "（查询失败）"

    command_line = decode_console_bytes(completed.stdout).strip()
    return command_line or "（无法获取，可能需要管理员权限）"


class PortManagerApplication:
    """端口占用管理主窗口。

    采集与终止都在后台线程执行，结果通过队列回传主线程，
    避免 netstat/taskkill 的等待把界面卡住。
    """

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.background_result_queue: queue.Queue[tuple[str, object]] = queue.Queue()

        self.all_bindings: list[PortBinding] = []
        self.displayed_bindings: list[PortBinding] = []
        self.sort_column_name = "port"
        self.sort_descending = False
        self.is_task_running = False
        self.is_closing = False
        self.scheduled_auto_refresh_id: str | None = None
        self.scheduled_poll_id: str | None = None

        self.search_keyword = tk.StringVar()
        self.protocol_filter = tk.StringVar(value=PROTOCOL_FILTER_ALL)
        self.show_listening_only = tk.BooleanVar(value=True)
        self.hide_system_processes = tk.BooleanVar(value=True)
        self.show_dev_ports_only = tk.BooleanVar(value=False)
        self.auto_refresh_enabled = tk.BooleanVar(value=False)
        self.auto_refresh_interval_label = tk.StringVar(value=DEFAULT_AUTO_REFRESH_LABEL)
        self.quick_release_port = tk.StringVar()
        self.status_message = tk.StringVar(value="正在读取端口占用…")

        self._configure_window()
        self._configure_styles()
        self._build_toolbar()
        self._build_table()
        self._build_action_bar()
        self._build_status_bar()
        self._bind_shortcuts()

        self.scheduled_poll_id = self.root.after(
            BACKGROUND_POLL_INTERVAL_MS, self._poll_background_results
        )
        self.request_refresh()

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    def _configure_window(self) -> None:
        self.root.title(WINDOW_TITLE)
        self.root.minsize(880, 520)
        self.root.configure(background=COLOR_WINDOW_BACKGROUND)
        apply_window_icon(self.root)
        self._center_window(1040, 660)

    def _center_window(self, width: int, height: int) -> None:
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        left = max(0, (screen_width - width) // 2)
        top = max(0, (screen_height - height) // 3)
        self.root.geometry(f"{width}x{height}+{left}+{top}")

    def _create_checkbutton(
        self, parent: tk.Misc, text: str, variable: tk.BooleanVar, command: Callable[[], None]
    ) -> tk.Checkbutton:
        """使用原生 tk.Checkbutton 而非 ttk 版本。

        clam 主题把「已勾选」画成一个 ✕，容易被误读成「关闭/否」，
        原生控件显示的是常规对勾，语义更清晰。
        """
        return tk.Checkbutton(
            parent,
            text=text,
            variable=variable,
            command=command,
            font=self.ui_font,
            background=COLOR_WINDOW_BACKGROUND,
            activebackground=COLOR_WINDOW_BACKGROUND,
            foreground=COLOR_TEXT_PRIMARY,
            activeforeground=COLOR_TEXT_PRIMARY,
            selectcolor=COLOR_SURFACE,
            borderwidth=0,
            highlightthickness=0,
            cursor="hand2",
        )

    def _configure_styles(self) -> None:
        ui_font_family = pick_first_available_font(
            ["Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Segoe UI"], "TkDefaultFont"
        )
        monospace_font_family = pick_first_available_font(
            ["Consolas", "Cascadia Mono", "Menlo", "DejaVu Sans Mono"], "TkFixedFont"
        )
        self.ui_font = (ui_font_family, 10)
        self.ui_font_bold = (ui_font_family, 10, "bold")
        self.title_font = (ui_font_family, 15, "bold")
        self.table_font = (monospace_font_family, 10)

        style = ttk.Style(self.root)
        # clam 是少数允许自定义配色的内置主题，Windows 默认主题会忽略大部分颜色设置。
        if "clam" in style.theme_names():
            style.theme_use("clam")

        style.configure("TFrame", background=COLOR_WINDOW_BACKGROUND)
        style.configure("Surface.TFrame", background=COLOR_SURFACE)
        style.configure(
            "TLabel", background=COLOR_WINDOW_BACKGROUND, foreground=COLOR_TEXT_PRIMARY, font=self.ui_font
        )
        style.configure("Title.TLabel", font=self.title_font)
        style.configure("Muted.TLabel", foreground=COLOR_TEXT_MUTED)
        style.configure("TEntry", fieldbackground=COLOR_SURFACE, font=self.ui_font)
        style.configure("TCombobox", fieldbackground=COLOR_SURFACE, font=self.ui_font)

        style.configure("TButton", font=self.ui_font, padding=(12, 6))
        style.configure(
            "Accent.TButton",
            font=self.ui_font_bold,
            foreground="#ffffff",
            background=COLOR_ACCENT,
            padding=(14, 7),
            borderwidth=0,
        )
        style.map(
            "Accent.TButton",
            background=[("pressed", COLOR_ACCENT_ACTIVE), ("active", COLOR_ACCENT_ACTIVE), ("disabled", "#9db4ee")],
        )
        style.configure(
            "Danger.TButton",
            font=self.ui_font_bold,
            foreground="#ffffff",
            background=COLOR_DANGER,
            padding=(14, 7),
            borderwidth=0,
        )
        style.map(
            "Danger.TButton",
            background=[("pressed", COLOR_DANGER_ACTIVE), ("active", COLOR_DANGER_ACTIVE), ("disabled", "#eda3a3")],
        )

        style.configure(
            "Ports.Treeview",
            background=COLOR_SURFACE,
            fieldbackground=COLOR_SURFACE,
            foreground=COLOR_TEXT_PRIMARY,
            rowheight=27,
            font=self.table_font,
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "Ports.Treeview.Heading",
            font=self.ui_font_bold,
            background="#eceef1",
            foreground=COLOR_TEXT_PRIMARY,
            relief="flat",
            padding=(6, 8),
        )
        style.map("Ports.Treeview.Heading", background=[("active", "#e0e3e8")])
        style.map(
            "Ports.Treeview",
            background=[("selected", COLOR_ACCENT)],
            foreground=[("selected", "#ffffff")],
        )

    def _build_toolbar(self) -> None:
        header = ttk.Frame(self.root, padding=(16, 14, 16, 6))
        header.pack(fill="x")

        ttk.Label(header, text="端口占用管理", style="Title.TLabel").pack(side="left")
        ttk.Label(
            header,
            text="查看谁占用了端口，并安全地释放它",
            style="Muted.TLabel",
        ).pack(side="left", padx=(12, 0), pady=(4, 0))

        quick_release_area = ttk.Frame(header)
        quick_release_area.pack(side="right")
        ttk.Label(quick_release_area, text="快速释放端口").pack(side="left", padx=(0, 6))
        quick_release_entry = ttk.Entry(
            quick_release_area, textvariable=self.quick_release_port, width=10, justify="center"
        )
        quick_release_entry.pack(side="left")
        quick_release_entry.bind("<Return>", lambda event: self.release_port_from_entry())
        self.quick_release_button = ttk.Button(
            quick_release_area,
            text="释放",
            style="Danger.TButton",
            command=self.release_port_from_entry,
        )
        self.quick_release_button.pack(side="left", padx=(6, 0))

        filter_bar = ttk.Frame(self.root, padding=(16, 6, 16, 10))
        filter_bar.pack(fill="x")

        ttk.Label(filter_bar, text="搜索").pack(side="left")
        search_entry = ttk.Entry(filter_bar, textvariable=self.search_keyword, width=26)
        search_entry.pack(side="left", padx=(6, 14))
        search_entry.insert(0, "")
        self.search_keyword.trace_add("write", lambda *_: self._render_table())

        ttk.Label(filter_bar, text="协议").pack(side="left")
        protocol_combobox = ttk.Combobox(
            filter_bar,
            textvariable=self.protocol_filter,
            values=list(PROTOCOL_FILTER_CHOICES),
            width=6,
            state="readonly",
        )
        protocol_combobox.pack(side="left", padx=(6, 14))
        protocol_combobox.bind("<<ComboboxSelected>>", lambda event: self._render_table())

        self._create_checkbutton(
            filter_bar, "仅监听端口", self.show_listening_only, self._render_table
        ).pack(side="left", padx=(0, 12))
        self._create_checkbutton(
            filter_bar, "隐藏系统进程", self.hide_system_processes, self._render_table
        ).pack(side="left", padx=(0, 12))
        self._create_checkbutton(
            filter_bar, "只看开发端口", self.show_dev_ports_only, self._render_table
        ).pack(side="left")

        auto_refresh_area = ttk.Frame(filter_bar)
        auto_refresh_area.pack(side="right")
        interval_combobox = ttk.Combobox(
            auto_refresh_area,
            textvariable=self.auto_refresh_interval_label,
            values=list(AUTO_REFRESH_INTERVAL_CHOICES),
            width=6,
            state="readonly",
        )
        interval_combobox.pack(side="right", padx=(6, 0))
        interval_combobox.bind("<<ComboboxSelected>>", lambda event: self._reschedule_auto_refresh())
        self._create_checkbutton(
            auto_refresh_area, "自动刷新", self.auto_refresh_enabled, self._reschedule_auto_refresh
        ).pack(side="right")

    def _build_table(self) -> None:
        table_container = ttk.Frame(self.root, padding=(16, 0))
        table_container.pack(fill="both", expand=True)

        self.table = ttk.Treeview(
            table_container,
            columns=[column_id for column_id, _, _, _ in TABLE_COLUMNS],
            show="headings",
            selectmode="extended",
            style="Ports.Treeview",
        )
        for column_id, heading_text, column_width, anchor in TABLE_COLUMNS:
            self.table.heading(
                column_id,
                text=heading_text,
                anchor=anchor,
                command=lambda name=column_id: self._toggle_sort_by_column(name),
            )
            self.table.column(column_id, width=column_width, anchor=anchor, stretch=(column_id == "process"))

        vertical_scrollbar = ttk.Scrollbar(table_container, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=vertical_scrollbar.set)
        self.table.pack(side="left", fill="both", expand=True)
        vertical_scrollbar.pack(side="right", fill="y")

        self.table.tag_configure("stripe", background=COLOR_STRIPE_BACKGROUND)
        self.table.tag_configure(
            "system_process", background=COLOR_SYSTEM_ROW_BACKGROUND, foreground=COLOR_SYSTEM_ROW_TEXT
        )
        self.table.tag_configure("kernel_placeholder", foreground=COLOR_KERNEL_ROW_TEXT)

        self.table.bind("<Double-1>", lambda event: self.show_selected_process_details())
        self.table.bind("<Button-3>", self._show_context_menu)
        self.table.bind("<<TreeviewSelect>>", lambda event: self._update_action_button_states())

        self.context_menu = tk.Menu(self.root, tearoff=0, font=self.ui_font)
        self.context_menu.add_command(label="终止选中进程", command=self.terminate_selected_rows)
        self.context_menu.add_command(
            label="强制终止（跳过保护）", command=lambda: self.terminate_selected_rows(force=True)
        )
        self.context_menu.add_separator()
        self.context_menu.add_command(label="查看进程详情", command=self.show_selected_process_details)
        self.context_menu.add_command(label="复制这一行", command=self.copy_selected_rows)

    def _build_action_bar(self) -> None:
        action_bar = ttk.Frame(self.root, padding=(16, 12, 16, 8))
        action_bar.pack(fill="x")

        self.refresh_button = ttk.Button(
            action_bar, text="刷新 (F5)", style="Accent.TButton", command=self.request_refresh
        )
        self.refresh_button.pack(side="left")

        self.terminate_button = ttk.Button(
            action_bar, text="终止选中进程", style="Danger.TButton", command=self.terminate_selected_rows
        )
        self.terminate_button.pack(side="left", padx=(10, 0))

        self.details_button = ttk.Button(
            action_bar, text="进程详情", command=self.show_selected_process_details
        )
        self.details_button.pack(side="left", padx=(10, 0))

        self.copy_button = ttk.Button(action_bar, text="复制", command=self.copy_selected_rows)
        self.copy_button.pack(side="left", padx=(10, 0))

        ttk.Label(
            action_bar,
            text="双击查看详情 · 右键更多操作 · 支持多选批量终止",
            style="Muted.TLabel",
        ).pack(side="right")

        self._update_action_button_states()

    def _build_status_bar(self) -> None:
        status_bar = ttk.Frame(self.root, padding=(16, 0, 16, 12))
        status_bar.pack(fill="x")
        ttk.Label(status_bar, textvariable=self.status_message, style="Muted.TLabel").pack(side="left")

    def _bind_shortcuts(self) -> None:
        self.root.bind("<F5>", lambda event: self.request_refresh())
        self.root.bind("<Control-r>", lambda event: self.request_refresh())
        self.root.bind("<Delete>", lambda event: self.terminate_selected_rows())
        self.root.protocol("WM_DELETE_WINDOW", self.handle_window_close)

    # ------------------------------------------------------------------
    # 数据刷新
    # ------------------------------------------------------------------
    def request_refresh(self) -> None:
        if self.is_task_running:
            return
        self._set_task_running(True, "正在读取端口占用…")
        self._run_in_background(self._collect_bindings_task)

    def _collect_bindings_task(self) -> None:
        try:
            bindings = collect_port_bindings()
        except PortToolError as error:
            self.background_result_queue.put(("error", str(error)))
            return
        except Exception as error:
            self.background_result_queue.put(("error", f"读取端口信息时出错：{error}"))
            return
        self.background_result_queue.put(("bindings", bindings))

    def _run_in_background(self, task: Callable[[], None]) -> None:
        worker = threading.Thread(target=task, daemon=True)
        worker.start()

    def _poll_background_results(self) -> None:
        """在主线程消费后台结果；tkinter 控件只能由主线程操作。"""
        if self.is_closing:
            return
        try:
            while True:
                result_kind, payload = self.background_result_queue.get_nowait()
                if result_kind == "bindings":
                    self.all_bindings = list(payload)  # type: ignore[arg-type]
                    self._render_table()
                    self._set_task_running(False)
                elif result_kind == "error":
                    self._set_task_running(False)
                    self.status_message.set(f"读取失败：{payload}")
                    messagebox.showerror("读取端口信息失败", str(payload), parent=self.root)
                elif result_kind == "termination_report":
                    self._set_task_running(False)
                    self._show_termination_report(payload)  # type: ignore[arg-type]
                    self.request_refresh()
        except queue.Empty:
            pass
        except tk.TclError:
            # 窗口已被销毁（例如直接调用 destroy），停止后续轮询。
            self.is_closing = True
            return
        finally:
            if not self.is_closing:
                try:
                    self.scheduled_poll_id = self.root.after(
                        BACKGROUND_POLL_INTERVAL_MS, self._poll_background_results
                    )
                except tk.TclError:
                    self.is_closing = True

    def handle_window_close(self) -> None:
        """关闭窗口前取消所有已排程回调，避免访问已销毁的控件。"""
        self.is_closing = True
        for scheduled_id in (self.scheduled_auto_refresh_id, self.scheduled_poll_id):
            if scheduled_id is not None:
                try:
                    self.root.after_cancel(scheduled_id)
                except tk.TclError:
                    pass
        self.scheduled_auto_refresh_id = None
        self.scheduled_poll_id = None
        self.root.destroy()

    def _reschedule_auto_refresh(self) -> None:
        if self.scheduled_auto_refresh_id is not None:
            self.root.after_cancel(self.scheduled_auto_refresh_id)
            self.scheduled_auto_refresh_id = None
        if not self.auto_refresh_enabled.get():
            return
        interval_ms = AUTO_REFRESH_INTERVAL_CHOICES[self.auto_refresh_interval_label.get()]
        self.scheduled_auto_refresh_id = self.root.after(interval_ms, self._run_auto_refresh_tick)

    def _run_auto_refresh_tick(self) -> None:
        self.request_refresh()
        self._reschedule_auto_refresh()

    def _set_task_running(self, running: bool, message: str | None = None) -> None:
        self.is_task_running = running
        widget_state = "disabled" if running else "normal"
        for button in (self.refresh_button, self.quick_release_button):
            button.configure(state=widget_state)
        if message:
            self.status_message.set(message)
        self._update_action_button_states()

    # ------------------------------------------------------------------
    # 表格渲染
    # ------------------------------------------------------------------
    def _collect_visible_bindings(self) -> list[PortBinding]:
        protocol_choice = self.protocol_filter.get()
        visible = filter_bindings(
            self.all_bindings,
            ports=COMMON_DEV_PORTS if self.show_dev_ports_only.get() else None,
            protocol=None if protocol_choice == PROTOCOL_FILTER_ALL else protocol_choice,
            listening_only=self.show_listening_only.get(),
        )

        if self.hide_system_processes.get():
            visible = [binding for binding in visible if not binding.is_protected]

        keyword = self.search_keyword.get().strip().lower()
        if keyword:
            visible = [binding for binding in visible if self._binding_matches_keyword(binding, keyword)]

        return self._sort_bindings(visible)

    def _binding_matches_keyword(self, binding: PortBinding, keyword: str) -> bool:
        searchable_text = " ".join(
            [
                str(binding.local_port),
                str(binding.pid),
                binding.process_name.lower(),
                binding.protocol.lower(),
                binding.local_address.lower(),
                binding.state.lower(),
            ]
        )
        return keyword in searchable_text

    def _sort_bindings(self, bindings: list[PortBinding]) -> list[PortBinding]:
        sort_key_builders: dict[str, Callable[[PortBinding], object]] = {
            "port": lambda binding: (binding.local_port, binding.protocol),
            "protocol": lambda binding: (binding.protocol, binding.local_port),
            "state": lambda binding: (binding.state, binding.local_port),
            "pid": lambda binding: (binding.pid, binding.local_port),
            "process": lambda binding: (binding.process_name.lower(), binding.local_port),
            "address": lambda binding: (binding.local_address, binding.local_port),
        }
        sort_key = sort_key_builders.get(self.sort_column_name, sort_key_builders["port"])
        return sorted(bindings, key=sort_key, reverse=self.sort_descending)  # type: ignore[arg-type]

    def _toggle_sort_by_column(self, column_name: str) -> None:
        if self.sort_column_name == column_name:
            self.sort_descending = not self.sort_descending
        else:
            self.sort_column_name = column_name
            self.sort_descending = False
        self._render_table()

    def _render_table(self) -> None:
        previously_selected_keys = {
            self._build_row_key(binding) for binding in self._get_selected_bindings()
        }

        self.displayed_bindings = self._collect_visible_bindings()
        self.table.delete(*self.table.get_children())

        for row_index, binding in enumerate(self.displayed_bindings):
            row_tags: list[str] = []
            if binding.pid <= 0:
                process_label = "内核残留连接"
                row_tags.append("kernel_placeholder")
            elif binding.is_protected:
                process_label = f"{binding.process_name}  [系统]"
                row_tags.append("system_process")
            else:
                process_label = binding.process_name
                if row_index % 2 == 1:
                    row_tags.append("stripe")

            self.table.insert(
                "",
                "end",
                iid=str(row_index),
                values=(
                    binding.local_port,
                    binding.protocol,
                    binding.state or "-",
                    binding.pid,
                    process_label,
                    binding.local_address,
                ),
                tags=tuple(row_tags),
            )

        self._restore_selection(previously_selected_keys)
        self._update_column_heading_indicators()
        self._update_status_summary()
        self._update_action_button_states()

    def _build_row_key(self, binding: PortBinding) -> tuple[str, int, int]:
        return (binding.protocol, binding.local_port, binding.pid)

    def _restore_selection(self, previously_selected_keys: set[tuple[str, int, int]]) -> None:
        if not previously_selected_keys:
            return
        row_ids_to_select = [
            str(row_index)
            for row_index, binding in enumerate(self.displayed_bindings)
            if self._build_row_key(binding) in previously_selected_keys
        ]
        if row_ids_to_select:
            self.table.selection_set(row_ids_to_select)

    def _update_column_heading_indicators(self) -> None:
        for column_id, heading_text, _, _ in TABLE_COLUMNS:
            indicator = ""
            if column_id == self.sort_column_name:
                indicator = " ▼" if self.sort_descending else " ▲"
            self.table.heading(column_id, text=f"{heading_text}{indicator}")

    def _update_status_summary(self) -> None:
        killable_count = sum(
            1 for binding in self.displayed_bindings if binding.pid > 0 and not binding.is_protected
        )
        protected_count = sum(
            1 for binding in self.displayed_bindings if binding.pid > 0 and binding.is_protected
        )
        kernel_placeholder_count = sum(1 for binding in self.displayed_bindings if binding.pid <= 0)

        summary_parts = [f"显示 {len(self.displayed_bindings)} 条", f"可终止 {killable_count}"]
        if protected_count:
            summary_parts.append(f"系统保护 {protected_count}")
        if kernel_placeholder_count:
            summary_parts.append(f"内核残留 {kernel_placeholder_count}")
        summary_parts.append(f"系统共 {len(self.all_bindings)} 条记录")
        summary_parts.append(f"最后刷新 {time.strftime('%H:%M:%S')}")

        self.status_message.set(" · ".join(summary_parts))

    def _update_action_button_states(self) -> None:
        selected_bindings = self._get_selected_bindings()
        has_killable_selection = any(binding.pid > 0 for binding in selected_bindings)
        allow_actions = not self.is_task_running

        self.terminate_button.configure(
            state="normal" if allow_actions and has_killable_selection else "disabled"
        )
        self.details_button.configure(state="normal" if selected_bindings else "disabled")
        self.copy_button.configure(state="normal" if selected_bindings else "disabled")

    def _show_context_menu(self, event: tk.Event) -> None:
        clicked_row_id = self.table.identify_row(event.y)
        if clicked_row_id and clicked_row_id not in self.table.selection():
            self.table.selection_set(clicked_row_id)
        if not self.table.selection():
            return
        self.context_menu.tk_popup(event.x_root, event.y_root)

    def _get_selected_bindings(self) -> list[PortBinding]:
        selected_bindings: list[PortBinding] = []
        for row_id in self.table.selection():
            try:
                selected_bindings.append(self.displayed_bindings[int(row_id)])
            except (ValueError, IndexError):
                continue
        return selected_bindings

    # ------------------------------------------------------------------
    # 终止操作
    # ------------------------------------------------------------------
    def terminate_selected_rows(self, force: bool = False) -> None:
        selected_bindings = self._get_selected_bindings()
        if not selected_bindings:
            messagebox.showinfo("未选择", "请先在列表中选择要终止的行。", parent=self.root)
            return
        self._terminate_bindings(selected_bindings, force=force)

    def release_port_from_entry(self) -> None:
        port_text = self.quick_release_port.get().strip()
        if not port_text.isdigit() or not (0 < int(port_text) <= 65535):
            messagebox.showwarning("端口无效", "请输入 1-65535 之间的端口号。", parent=self.root)
            return

        port = int(port_text)
        matched_bindings = filter_bindings(self.all_bindings, ports=[port], listening_only=False)
        if not matched_bindings:
            messagebox.showinfo(
                "端口空闲",
                f"端口 {port} 当前没有被占用。\n\n如果刚刚才释放，可点击刷新确认最新状态。",
                parent=self.root,
            )
            return
        self._terminate_bindings(matched_bindings, force=False)

    def _terminate_bindings(self, bindings: Sequence[PortBinding], force: bool) -> None:
        if self.is_task_running:
            return

        killable_bindings = [binding for binding in bindings if binding.pid > 0]
        if not killable_bindings:
            messagebox.showinfo(
                "无可终止进程",
                "所选记录只有 TIME_WAIT 等内核残留连接，没有对应进程。\n"
                "这类占用通常几十秒后会自动释放。",
                parent=self.root,
            )
            return

        protected_bindings = [binding for binding in killable_bindings if binding.is_protected]
        if protected_bindings and not force:
            protected_summary = "\n".join(
                f"  · {binding.process_name}（PID {binding.pid}，端口 {binding.local_port}）"
                for binding in protected_bindings
            )
            messagebox.showwarning(
                "已跳过系统关键进程",
                "以下进程属于操作系统关键组件，终止它们可能导致系统不稳定，已被保护：\n\n"
                f"{protected_summary}\n\n"
                "确实需要终止时，请使用右键菜单中的「强制终止（跳过保护）」。",
                parent=self.root,
            )
            killable_bindings = [binding for binding in killable_bindings if not binding.is_protected]
            if not killable_bindings:
                return

        unique_targets: dict[int, PortBinding] = {}
        for binding in killable_bindings:
            unique_targets.setdefault(binding.pid, binding)

        target_summary = "\n".join(
            f"  · {binding.process_name}（PID {binding.pid}，端口 {binding.local_port}）"
            for binding in unique_targets.values()
        )
        action_description = "强制终止" if force else "终止"
        if not messagebox.askyesno(
            f"确认{action_description}",
            f"即将{action_description}以下 {len(unique_targets)} 个进程：\n\n{target_summary}\n\n"
            + ("强制终止会立即结束进程，未保存的数据会丢失。" if force else "会先请求进程正常退出，超时后再强制结束。")
            + "\n\n是否继续？",
            parent=self.root,
        ):
            return

        affected_ports = sorted({binding.local_port for binding in killable_bindings})
        self._set_task_running(True, f"正在{action_description} {len(unique_targets)} 个进程…")
        self._run_in_background(
            lambda: self._terminate_targets_task(list(unique_targets.values()), affected_ports, force)
        )

    def _terminate_targets_task(
        self, targets: Sequence[PortBinding], affected_ports: Sequence[int], force: bool
    ) -> None:
        outcomes: list[TerminationOutcome] = []
        for binding in targets:
            try:
                succeeded, detail = terminate_process(binding.pid, force=force, include_children=False)
            except Exception as error:
                succeeded, detail = False, f"执行出错：{error}"
            outcomes.append(
                TerminationOutcome(
                    pid=binding.pid,
                    process_name=binding.process_name,
                    succeeded=succeeded,
                    detail=detail,
                )
            )

        released_ports: list[int] = []
        still_occupied_ports: list[int] = []
        for port in affected_ports:
            if wait_until_port_released(port, timeout_seconds=3.0):
                released_ports.append(port)
            else:
                still_occupied_ports.append(port)

        self.background_result_queue.put(
            ("termination_report", (outcomes, released_ports, still_occupied_ports))
        )

    def _show_termination_report(
        self, report: tuple[list[TerminationOutcome], list[int], list[int]]
    ) -> None:
        outcomes, released_ports, still_occupied_ports = report

        succeeded_outcomes = [outcome for outcome in outcomes if outcome.succeeded]
        failed_outcomes = [outcome for outcome in outcomes if not outcome.succeeded]

        message_lines: list[str] = []
        if succeeded_outcomes:
            message_lines.append(f"已终止 {len(succeeded_outcomes)} 个进程：")
            message_lines.extend(
                f"  · {outcome.process_name}（PID {outcome.pid}）{outcome.detail}"
                for outcome in succeeded_outcomes
            )
        if failed_outcomes:
            if message_lines:
                message_lines.append("")
            message_lines.append(f"{len(failed_outcomes)} 个进程终止失败：")
            message_lines.extend(
                f"  · {outcome.process_name}（PID {outcome.pid}）{outcome.detail}"
                for outcome in failed_outcomes
            )
        if released_ports:
            message_lines.append("")
            message_lines.append("已释放端口：" + "、".join(map(str, released_ports)))
        if still_occupied_ports:
            message_lines.append("")
            message_lines.append(
                "仍显示占用的端口：" + "、".join(map(str, still_occupied_ports))
                + "\n（可能处于 TIME_WAIT 等待关闭，稍后会自动释放）"
            )

        message_text = "\n".join(message_lines) or "没有执行任何操作。"
        if failed_outcomes:
            messagebox.showerror("终止结果", message_text, parent=self.root)
        else:
            messagebox.showinfo("终止结果", message_text, parent=self.root)

    # ------------------------------------------------------------------
    # 辅助操作
    # ------------------------------------------------------------------
    def show_selected_process_details(self) -> None:
        selected_bindings = self._get_selected_bindings()
        if not selected_bindings:
            return

        binding = selected_bindings[0]
        self.status_message.set(f"正在查询 PID {binding.pid} 的进程信息…")
        self.root.update_idletasks()
        command_line = fetch_process_command_line(binding.pid)

        details_text = (
            f"端口：{binding.local_port}／{binding.protocol}\n"
            f"状态：{binding.state or '-'}\n"
            f"监听地址：{binding.local_address}\n"
            f"PID：{binding.pid}\n"
            f"进程名：{binding.process_name}\n"
            f"系统关键进程：{'是（默认受保护）' if binding.is_protected else '否'}\n\n"
            f"命令行：\n{command_line}"
        )
        self._show_details_window(f"进程详情 — {binding.process_name}", details_text)
        self._update_status_summary()

    def _show_details_window(self, title: str, content: str) -> None:
        details_window = tk.Toplevel(self.root)
        details_window.title(title)
        details_window.geometry("720x360")
        details_window.configure(background=COLOR_WINDOW_BACKGROUND)
        details_window.transient(self.root)

        text_area = tk.Text(
            details_window,
            wrap="word",
            font=self.table_font,
            background=COLOR_SURFACE,
            foreground=COLOR_TEXT_PRIMARY,
            relief="flat",
            padx=14,
            pady=12,
            borderwidth=1,
        )
        text_area.insert("1.0", content)
        text_area.configure(state="disabled")
        text_area.pack(fill="both", expand=True, padx=14, pady=(14, 8))

        button_row = ttk.Frame(details_window, padding=(14, 0, 14, 14))
        button_row.pack(fill="x")
        ttk.Button(
            button_row,
            text="复制内容",
            command=lambda: self._copy_text_to_clipboard(content, "进程详情已复制到剪贴板"),
        ).pack(side="left")
        ttk.Button(button_row, text="关闭", style="Accent.TButton", command=details_window.destroy).pack(
            side="right"
        )

    def copy_selected_rows(self) -> None:
        selected_bindings = self._get_selected_bindings()
        if not selected_bindings:
            return
        text_lines = [
            f"{binding.local_port}\t{binding.protocol}\t{binding.state or '-'}\t"
            f"{binding.pid}\t{binding.process_name}\t{binding.local_address}"
            for binding in selected_bindings
        ]
        self._copy_text_to_clipboard("\n".join(text_lines), f"已复制 {len(text_lines)} 行到剪贴板")

    def _copy_text_to_clipboard(self, text: str, status_hint: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_message.set(status_hint)


def main() -> int:
    enable_high_dpi_awareness()
    root = tk.Tk()
    PortManagerApplication(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
