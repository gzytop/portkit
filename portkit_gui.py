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
from theme import (
    DARK_PALETTE,
    FONT_SIZE_CAPTION,
    FONT_SIZE_BODY,
    FONT_SIZE_TABLE,
    FONT_SIZE_TITLE,
    LIGHT_PALETTE,
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    SPACE_XL,
    SPACE_XS,
    TABLE_ROW_HEIGHT,
    Palette,
)

WINDOW_TITLE = "portkit — 端口占用管理"
PROTOCOL_FILTER_ALL = "全部"
PROTOCOL_FILTER_CHOICES = (PROTOCOL_FILTER_ALL, "TCP", "UDP")
AUTO_REFRESH_INTERVAL_CHOICES = {"2 秒": 2000, "5 秒": 5000, "10 秒": 10000, "30 秒": 30000}
DEFAULT_AUTO_REFRESH_LABEL = "5 秒"
BACKGROUND_POLL_INTERVAL_MS = 100

# 表格列。列序按用户的判断顺序排：先看端口，再看谁占着，然后判断能不能动它。
TABLE_COLUMNS = (
    ("port", "端口", 78, "e"),
    ("protocol", "协议", 62, "center"),
    ("state", "连接状态", 100, "center"),
    ("pid", "PID", 72, "e"),
    ("process", "进程", 210, "w"),
    ("disposition", "处置", 84, "center"),
    ("address", "监听地址", 138, "w"),
)

# 每一行的语义分类。决定处置列文案、行底色与是否可终止，
# 集中在一处定义，避免渲染逻辑与终止逻辑对「能不能杀」判断不一致。
DISPOSITION_KILLABLE = "killable"
DISPOSITION_PROTECTED = "protected"
DISPOSITION_KERNEL = "kernel"

# 分类的完整名称，用于确认弹窗、报告等需要成句表达的地方。
DISPOSITION_LABELS = {
    DISPOSITION_KILLABLE: "可终止",
    DISPOSITION_PROTECTED: "系统保护",
    DISPOSITION_KERNEL: "内核残留",
}

# 表格里显示的文案：可终止是常态，整列写满「可终止」等于一列噪音，
# 既占宽度又没有信息量。只有「动不了」这种例外情况才需要明确写出来。
# 无障碍要求仍然满足——需要提醒的两类状态都有文字，不是只靠颜色区分。
DISPOSITION_TABLE_LABELS = {
    DISPOSITION_KILLABLE: "",
    DISPOSITION_PROTECTED: "系统保护",
    DISPOSITION_KERNEL: "内核残留",
}


def classify_binding(binding: PortBinding) -> str:
    """判断一条记录属于哪种处置类别。"""
    if binding.pid <= 0:
        return DISPOSITION_KERNEL
    if binding.is_protected:
        return DISPOSITION_PROTECTED
    return DISPOSITION_KILLABLE


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

    def __init__(self, root: tk.Tk, palette: Palette | None = None) -> None:
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

        # 主题相关状态。原生 tk 控件不受 ttk 样式管理，必须登记后逐个重着色。
        self.palette = palette or LIGHT_PALETTE
        self.native_checkbuttons: list[tk.Checkbutton] = []
        # 当前鼠标悬停的行。Treeview 没有 :hover，只能自己跟踪。
        self.hovered_row_id: str | None = None

        self.search_keyword = tk.StringVar()
        self.protocol_filter = tk.StringVar(value=PROTOCOL_FILTER_ALL)
        self.show_listening_only = tk.BooleanVar(value=True)
        self.hide_system_processes = tk.BooleanVar(value=True)
        self.show_dev_ports_only = tk.BooleanVar(value=False)
        self.auto_refresh_enabled = tk.BooleanVar(value=False)
        self.auto_refresh_interval_label = tk.StringVar(value=DEFAULT_AUTO_REFRESH_LABEL)
        self.quick_release_port = tk.StringVar()
        self.status_message = tk.StringVar(value="正在读取端口占用…")
        self.status_detail_message = tk.StringVar(value="")

        self._resolve_fonts()
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
    # 主题
    # ------------------------------------------------------------------
    def toggle_theme(self) -> None:
        """在亮色与暗色之间切换。"""
        self.apply_palette(DARK_PALETTE if not self.palette.is_dark else LIGHT_PALETTE)

    def apply_palette(self, palette: Palette) -> None:
        """换用新色板并把界面上所有受影响的部分重新着色。"""
        self.palette = palette
        self.root.configure(background=palette.window)
        self._configure_styles()

        for checkbutton in self.native_checkbuttons:
            self._paint_checkbutton(checkbutton)

        self._paint_context_menu()
        self._configure_table_row_tags()
        self._update_theme_button_label()
        # 原生 tk.Frame 不受 ttk 样式管理，描边色要手动跟上。
        if hasattr(self, "quick_release_card_border"):
            self.quick_release_card_border.configure(background=palette.border)
        # 行底色写在 tag 上，必须重画表格才会生效。
        self._render_table()

    def _update_theme_button_label(self) -> None:
        """按钮文案指向「切换后会得到什么」，而不是当前状态，避免歧义。"""
        if hasattr(self, "theme_button"):
            self.theme_button.configure(text="暗色" if not self.palette.is_dark else "亮色")

    def _paint_context_menu(self) -> None:
        if not hasattr(self, "context_menu"):
            return
        self.context_menu.configure(
            background=self.palette.surface,
            foreground=self.palette.text_primary,
            activebackground=self.palette.accent,
            activeforeground=self.palette.accent_text,
            font=self.ui_font,
            borderwidth=1,
        )

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    def _configure_window(self) -> None:
        self.root.title(WINDOW_TITLE)
        self.root.minsize(940, 560)
        self.root.configure(background=self.palette.window)
        apply_window_icon(self.root)
        self._center_window(1080, 700)

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

        原生控件不受 ttk 样式管理，所以要登记下来，切换主题时逐个重着色。
        """
        checkbutton = tk.Checkbutton(
            parent,
            text=text,
            variable=variable,
            command=command,
            borderwidth=0,
            highlightthickness=0,
            cursor="hand2",
        )
        self.native_checkbuttons.append(checkbutton)
        self._paint_checkbutton(checkbutton)
        return checkbutton

    def _paint_checkbutton(self, checkbutton: tk.Checkbutton) -> None:
        checkbutton.configure(
            font=self.ui_font,
            background=self.palette.window,
            activebackground=self.palette.window,
            foreground=self.palette.text_primary,
            activeforeground=self.palette.text_primary,
            # 方框内部底色。勾选标记的颜色跟随 foreground，所以这两者
            # 必须有足够对比度，否则会出现「白对勾画在亮灰底上」的情况——
            # 暗色主题曾因此让三个复选框看起来完全一样，勾没勾无法分辨。
            # 该组合已纳入 theme.critical_contrast_pairs 的自动断言。
            selectcolor=self.palette.control_field,
        )

    def _resolve_fonts(self) -> None:
        """挑选字体族并按阶梯建立字号。

        中文界面下把 UI 文本与表格数字分开处理：正文用系统中文字体保证字形，
        表格用等宽字体让端口号和 PID 纵向对齐——这类数字表格对齐后扫读快得多。
        """
        ui_font_family = pick_first_available_font(
            ["Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Segoe UI"], "TkDefaultFont"
        )
        monospace_font_family = pick_first_available_font(
            ["Cascadia Mono", "Consolas", "SF Mono", "Menlo", "DejaVu Sans Mono"], "TkFixedFont"
        )
        self.ui_font = (ui_font_family, FONT_SIZE_BODY)
        self.ui_font_bold = (ui_font_family, FONT_SIZE_BODY, "bold")
        self.caption_font = (ui_font_family, FONT_SIZE_CAPTION)
        self.title_font = (ui_font_family, FONT_SIZE_TITLE, "bold")
        self.table_font = (monospace_font_family, FONT_SIZE_TABLE)
        self.table_heading_font = (ui_font_family, FONT_SIZE_CAPTION, "bold")

    def _configure_styles(self) -> None:
        """把当前色板写进 ttk 样式。切换主题时会再次调用。"""
        palette = self.palette
        style = ttk.Style(self.root)
        # clam 是少数允许自定义配色的内置主题，Windows 默认主题会忽略大部分颜色设置。
        if "clam" in style.theme_names():
            style.theme_use("clam")

        style.configure("TFrame", background=palette.window)
        style.configure("Surface.TFrame", background=palette.surface)
        style.configure("Toolbar.TFrame", background=palette.surface_raised)
        # 快速释放卡片的内层底色。用「表面色 + 一像素描边」把它从页头里抬出来，
        # 让主路径成为一块有边界的操作区，而不是散落在页头右侧的两个控件。
        style.configure("Card.TFrame", background=palette.surface)
        # 用一像素实色 Frame 当分隔线，比 ttk.Separator 在 clam 下更可控。
        style.configure("Divider.TFrame", background=palette.border)

        style.configure(
            "TLabel", background=palette.window, foreground=palette.text_primary, font=self.ui_font
        )
        style.configure("Toolbar.TLabel", background=palette.surface_raised)
        style.configure(
            "Title.TLabel",
            background=palette.surface_raised,
            foreground=palette.text_primary,
            font=self.title_font,
        )
        style.configure(
            "Subtitle.TLabel",
            background=palette.surface_raised,
            foreground=palette.text_secondary,
            font=self.ui_font,
        )
        # 卡片内的小标题：落在 surface 上而不是 surface_raised 上，底色必须跟着换。
        style.configure(
            "CardLabel.TLabel",
            background=palette.surface,
            foreground=palette.text_secondary,
            font=self.caption_font,
        )
        style.configure(
            "Muted.TLabel",
            background=palette.window,
            foreground=palette.text_secondary,
            font=self.ui_font,
        )
        style.configure(
            "Status.TLabel",
            background=palette.window,
            foreground=palette.text_secondary,
            font=self.caption_font,
        )
        # 状态栏分两级：主计数（显示多少条、可终止多少）要能一眼读到，
        # 「系统共 N 条 · 最后刷新」这类背景信息退到次要层级。
        # 原先整条状态栏同字号同颜色平铺，扫不出重点。
        style.configure(
            "StatusStrong.TLabel",
            background=palette.window,
            foreground=palette.text_primary,
            font=self.ui_font_bold,
        )
        style.configure(
            "FieldLabel.TLabel",
            background=palette.window,
            foreground=palette.text_secondary,
            font=self.ui_font,
        )

        style.configure(
            "TEntry",
            fieldbackground=palette.surface,
            foreground=palette.text_primary,
            insertcolor=palette.text_primary,
            bordercolor=palette.border,
            lightcolor=palette.border,
            darkcolor=palette.border,
            font=self.ui_font,
            padding=(SPACE_SM, SPACE_XS + 1),
        )
        style.map("TEntry", bordercolor=[("focus", palette.accent)])
        # 端口输入框是主路径入口，字号略大并加粗，视觉上给它应有的地位。
        style.configure(
            "Port.TEntry",
            fieldbackground=palette.surface,
            foreground=palette.text_primary,
            insertcolor=palette.text_primary,
            bordercolor=palette.border,
            font=(self.table_font[0], FONT_SIZE_BODY + 1, "bold"),
            padding=(SPACE_SM, SPACE_XS + 2),
        )
        style.map("Port.TEntry", bordercolor=[("focus", palette.accent)])

        style.configure(
            "TCombobox",
            fieldbackground=palette.surface,
            background=palette.surface,
            foreground=palette.text_primary,
            arrowcolor=palette.text_secondary,
            bordercolor=palette.border,
            lightcolor=palette.border,
            darkcolor=palette.border,
            font=self.ui_font,
            padding=(SPACE_XS, SPACE_XS),
        )
        # clam 下 readonly 的 Combobox 会退回主题默认配色，导致暗色主题里
        # 文字与底色撞成一片、看起来像空白框。必须显式映射每个状态。
        style.map(
            "TCombobox",
            fieldbackground=[
                ("readonly", palette.surface),
                ("disabled", palette.window),
            ],
            foreground=[
                ("readonly", palette.text_primary),
                ("disabled", palette.text_disabled),
            ],
            selectbackground=[("readonly", palette.surface)],
            selectforeground=[("readonly", palette.text_primary)],
            bordercolor=[("focus", palette.accent)],
            arrowcolor=[("disabled", palette.text_disabled)],
        )
        # 下拉列表是独立弹出窗口，不吃 style，只能通过全局 option 设置。
        self.root.option_add("*TCombobox*Listbox.background", palette.surface)
        self.root.option_add("*TCombobox*Listbox.foreground", palette.text_primary)
        self.root.option_add("*TCombobox*Listbox.selectBackground", palette.accent)
        self.root.option_add("*TCombobox*Listbox.selectForeground", palette.accent_text)
        self.root.option_add("*TCombobox*Listbox.font", self.ui_font)

        # 按钮分三级：危险（实心红）> 主要（实心蓝）> 次要（描边）。
        # 破坏性操作必须最重，避免与「刷新」这类无害操作视觉等价。
        style.configure(
            "Danger.TButton",
            font=self.ui_font_bold,
            foreground=palette.danger_text,
            background=palette.danger,
            bordercolor=palette.danger,
            focuscolor=palette.danger_text,
            padding=(SPACE_LG, SPACE_SM),
            borderwidth=0,
            relief="flat",
        )
        style.map(
            "Danger.TButton",
            background=[
                ("disabled", palette.control_disabled),
                ("pressed", palette.danger_hover),
                ("active", palette.danger_hover),
            ],
            foreground=[("disabled", palette.control_disabled_text)],
        )

        style.configure(
            "Accent.TButton",
            font=self.ui_font_bold,
            foreground=palette.accent_text,
            background=palette.accent,
            bordercolor=palette.accent,
            focuscolor=palette.accent_text,
            padding=(SPACE_LG, SPACE_SM),
            borderwidth=0,
            relief="flat",
        )
        style.map(
            "Accent.TButton",
            background=[
                ("disabled", palette.control_disabled),
                ("pressed", palette.accent_hover),
                ("active", palette.accent_hover),
            ],
            foreground=[("disabled", palette.control_disabled_text)],
        )

        style.configure(
            "Secondary.TButton",
            font=self.ui_font,
            foreground=palette.text_primary,
            background=palette.window,
            bordercolor=palette.border,
            focuscolor=palette.accent,
            padding=(SPACE_MD, SPACE_SM),
            borderwidth=1,
            relief="solid",
        )
        style.map(
            "Secondary.TButton",
            background=[("disabled", palette.window), ("active", palette.surface)],
            foreground=[("disabled", palette.control_disabled_text)],
            bordercolor=[("active", palette.accent), ("disabled", palette.control_disabled)],
        )

        style.configure(
            "Ports.Treeview",
            background=palette.surface,
            fieldbackground=palette.surface,
            foreground=palette.text_primary,
            rowheight=TABLE_ROW_HEIGHT,
            font=self.table_font,
            borderwidth=0,
            relief="flat",
        )
        # 表头压深并用正文色，让它比数据行更有分量。
        # 原先表头底色只比数据区亮一档、文字还是次要色，比数据本身更弱，
        # 整张表看不出「这是表头」，只是一片同色的行。
        style.configure(
            "Ports.Treeview.Heading",
            font=self.table_heading_font,
            background=palette.table_header,
            foreground=palette.text_primary,
            bordercolor=palette.border,
            relief="flat",
            padding=(SPACE_SM, SPACE_SM + 1),
        )
        style.map(
            "Ports.Treeview.Heading",
            background=[("active", palette.border)],
            foreground=[("active", palette.text_primary)],
        )
        style.map(
            "Ports.Treeview",
            background=[("selected", palette.selection)],
            foreground=[("selected", palette.selection_text)],
        )

        style.configure(
            "Vertical.TScrollbar",
            background=palette.surface_raised,
            troughcolor=palette.window,
            bordercolor=palette.window,
            arrowcolor=palette.text_secondary,
            relief="flat",
        )
        style.map("Vertical.TScrollbar", background=[("active", palette.border)])

    def _build_toolbar(self) -> None:
        """页头 + 筛选栏。

        页头用略高一层的底色并以一像素分隔线收边，让「品牌与主操作」
        和下方的「筛选条件」形成明确的分区，而不是所有控件平铺在同一片灰底上。
        """
        header = ttk.Frame(self.root, style="Toolbar.TFrame", padding=(SPACE_XL, SPACE_LG, SPACE_XL, SPACE_LG))
        header.pack(fill="x")

        title_area = ttk.Frame(header, style="Toolbar.TFrame")
        title_area.pack(side="left")
        ttk.Label(title_area, text="端口占用管理", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            title_area,
            text="查清是谁占着端口，然后安全地释放它",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(SPACE_XS, 0))

        # 主路径：输入端口 → 释放。包成一张描边卡片，让它成为页头里
        # 边界清晰的一块操作区，而不是右侧散着的一个输入框加一个红按钮。
        card_border = tk.Frame(header, background=self.palette.border, highlightthickness=0, borderwidth=0)
        card_border.pack(side="right")
        self.quick_release_card_border = card_border

        quick_release_area = ttk.Frame(
            card_border,
            style="Card.TFrame",
            padding=(SPACE_MD, SPACE_SM, SPACE_MD, SPACE_MD),
        )
        # 留 1px 边框：外层 Frame 的底色透出来即为描边。
        quick_release_area.pack(padx=1, pady=1)

        ttk.Label(
            quick_release_area, text="快速释放端口", style="CardLabel.TLabel"
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, SPACE_XS))

        quick_release_entry = ttk.Entry(
            quick_release_area,
            textvariable=self.quick_release_port,
            width=9,
            justify="center",
            style="Port.TEntry",
        )
        quick_release_entry.grid(row=1, column=0, sticky="ew")
        quick_release_entry.bind("<Return>", lambda event: self.release_port_from_entry())

        self.quick_release_button = ttk.Button(
            quick_release_area,
            text="释放",
            style="Danger.TButton",
            command=self.release_port_from_entry,
        )
        self.quick_release_button.grid(row=1, column=1, sticky="ew", padx=(SPACE_SM, 0))

        # 输入框此前没有任何提示，用户得先猜「这里填什么」。
        ttk.Label(
            quick_release_area,
            text="输入端口号后回车",
            style="CardLabel.TLabel",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(SPACE_XS, 0))

        ttk.Frame(self.root, style="Divider.TFrame", height=1).pack(fill="x")

        # 筛选栏只放「筛选条件」。自动刷新属于刷新行为、主题属于环境设置，
        # 分别归到操作栏与状态栏——这样每一栏都不会因控件过多而挤掉文字。
        filter_bar = ttk.Frame(self.root, padding=(SPACE_XL, SPACE_MD, SPACE_XL, SPACE_MD))
        filter_bar.pack(fill="x")

        ttk.Label(filter_bar, text="搜索", style="FieldLabel.TLabel").pack(side="left")
        search_entry = ttk.Entry(filter_bar, textvariable=self.search_keyword, width=22)
        search_entry.pack(side="left", padx=(SPACE_SM, SPACE_LG))
        self.search_keyword.trace_add("write", lambda *_: self._render_table())

        ttk.Label(filter_bar, text="协议", style="FieldLabel.TLabel").pack(side="left")
        protocol_combobox = ttk.Combobox(
            filter_bar,
            textvariable=self.protocol_filter,
            values=list(PROTOCOL_FILTER_CHOICES),
            width=6,
            state="readonly",
        )
        protocol_combobox.pack(side="left", padx=(SPACE_SM, SPACE_LG))
        protocol_combobox.bind("<<ComboboxSelected>>", lambda event: self._render_table())

        # 三个开关都在回答同一个问题：「这张表里显示哪些行」。
        # 收进带标签的一组，让它们读起来是一个整体，
        # 而不是与搜索、协议平铺在一起的三个孤立控件。
        ttk.Label(filter_bar, text="显示", style="FieldLabel.TLabel").pack(side="left")
        view_toggle_group = ttk.Frame(filter_bar)
        view_toggle_group.pack(side="left", padx=(SPACE_SM, 0))
        self._create_checkbutton(
            view_toggle_group, "仅监听端口", self.show_listening_only, self._render_table
        ).pack(side="left")
        self._create_checkbutton(
            view_toggle_group, "隐藏系统进程", self.hide_system_processes, self._render_table
        ).pack(side="left", padx=(SPACE_SM, 0))
        self._create_checkbutton(
            view_toggle_group, "只看开发端口", self.show_dev_ports_only, self._render_table
        ).pack(side="left", padx=(SPACE_SM, 0))

    def _build_table(self) -> None:
        table_container = ttk.Frame(self.root, padding=(SPACE_XL, 0))
        table_container.pack(fill="both", expand=True)

        # 给表格包一层描边，让它看起来是一块「内容面板」而不是浮在灰底上的散行。
        table_frame = tk.Frame(
            table_container,
            background=self.palette.border,
            highlightthickness=0,
            borderwidth=0,
        )
        table_frame.pack(fill="both", expand=True)
        self.table_frame = table_frame

        self.table = ttk.Treeview(
            table_frame,
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
            self.table.column(
                column_id,
                width=column_width,
                minwidth=column_width,
                anchor=anchor,
                stretch=(column_id in {"process", "address"}),
            )

        vertical_scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=vertical_scrollbar.set)
        # 留 1px 边框：外层 Frame 的底色透出来即为描边。
        self.table.pack(side="left", fill="both", expand=True, padx=(1, 0), pady=1)
        vertical_scrollbar.pack(side="right", fill="y", padx=(0, 1), pady=1)

        self._configure_table_row_tags()

        self.table.bind("<Double-1>", lambda event: self.show_selected_process_details())
        self.table.bind("<Button-3>", self._show_context_menu)
        self.table.bind("<<TreeviewSelect>>", lambda event: self._update_action_button_states())
        self.table.bind("<Motion>", self._handle_table_motion)
        self.table.bind("<Leave>", lambda event: self._clear_hovered_row())

        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="终止选中进程", command=self.terminate_selected_rows)
        self.context_menu.add_command(
            label="强制终止（跳过保护）", command=lambda: self.terminate_selected_rows(force=True)
        )
        self.context_menu.add_separator()
        self.context_menu.add_command(label="查看进程详情", command=self.show_selected_process_details)
        self.context_menu.add_command(label="复制这一行", command=self.copy_selected_rows)
        self._paint_context_menu()

    def _configure_table_row_tags(self) -> None:
        """按当前色板配置行标签。

        标签的着色只表达三件事，别的一律不着色：
          * 这一行是不是用户在找的开发端口（淡品牌底 + 加粗）
          * 这一行能不能杀（系统保护 → 琥珀；内核残留 → 降低对比度）
          * 斑马纹（纯粹辅助横向扫读）
        """
        palette = self.palette
        if hasattr(self, "table_frame"):
            self.table_frame.configure(background=palette.border)

        self.table.tag_configure("stripe", background=palette.row_stripe)
        self.table.tag_configure(
            "dev_port", background=palette.accent_soft, font=(self.table_font[0], FONT_SIZE_TABLE, "bold")
        )
        self.table.tag_configure(
            "system_process", background=palette.caution_soft, foreground=palette.caution
        )
        # 内核残留不可操作，压低对比度让它自然退到背景里，但仍保持可读。
        self.table.tag_configure("kernel_placeholder", foreground=palette.text_disabled)
        # 悬停行。tkinter 的 Treeview 没有 CSS :hover，只能跟踪鼠标所在行
        # 再换 tag。这条反馈能明确「点下去会作用在哪一行」，
        # 对一个会杀进程的列表来说值得做。
        self.table.tag_configure("hover", background=palette.row_hover)

    def _handle_table_motion(self, event: tk.Event) -> None:
        """鼠标移动时把 hover 标签挪到当前行。"""
        row_id_under_cursor = self.table.identify_row(event.y)
        if row_id_under_cursor == self.hovered_row_id:
            return
        self._clear_hovered_row()
        if row_id_under_cursor:
            self._add_row_tag(row_id_under_cursor, "hover")
            self.hovered_row_id = row_id_under_cursor

    def _clear_hovered_row(self) -> None:
        if self.hovered_row_id is None:
            return
        self._remove_row_tag(self.hovered_row_id, "hover")
        self.hovered_row_id = None

    def _add_row_tag(self, row_id: str, tag_name: str) -> None:
        try:
            existing_tags = list(self.table.item(row_id, "tags"))
        except tk.TclError:
            # 行可能已经因为刷新而消失。
            return
        if tag_name not in existing_tags:
            self.table.item(row_id, tags=tuple(existing_tags + [tag_name]))

    def _remove_row_tag(self, row_id: str, tag_name: str) -> None:
        try:
            existing_tags = list(self.table.item(row_id, "tags"))
        except tk.TclError:
            return
        if tag_name in existing_tags:
            self.table.item(row_id, tags=tuple(t for t in existing_tags if t != tag_name))

    def _build_action_bar(self) -> None:
        """操作栏。

        只保留两个按钮：终止（实心红）与刷新（实心蓝）。
        「进程详情」和「复制」原本也摆在这里，但它们都是针对某一行的操作——
        未选中行时是两个灰按钮，界面一打开就像坏了；而针对行的操作
        本来就该从行上发起，所以移进右键菜单，并在右侧写明入口。

        「自动刷新」紧跟在「刷新」右边——它修饰的是刷新行为，
        放在筛选栏里既不符合语义，也会因控件过多挤掉文字。
        """
        action_bar = ttk.Frame(self.root, padding=(SPACE_XL, SPACE_MD, SPACE_XL, SPACE_SM))
        action_bar.pack(fill="x")

        self.terminate_button = ttk.Button(
            action_bar,
            text="终止选中进程",
            style="Danger.TButton",
            command=self.terminate_selected_rows,
        )
        self.terminate_button.pack(side="left")

        self.refresh_button = ttk.Button(
            action_bar, text="刷新", style="Accent.TButton", command=self.request_refresh
        )
        self.refresh_button.pack(side="left", padx=(SPACE_MD, 0))

        auto_refresh_area = ttk.Frame(action_bar)
        auto_refresh_area.pack(side="left", padx=(SPACE_MD, 0))
        self._create_checkbutton(
            auto_refresh_area, "自动", self.auto_refresh_enabled, self._reschedule_auto_refresh
        ).pack(side="left")
        interval_combobox = ttk.Combobox(
            auto_refresh_area,
            textvariable=self.auto_refresh_interval_label,
            values=list(AUTO_REFRESH_INTERVAL_CHOICES),
            width=6,
            state="readonly",
        )
        interval_combobox.pack(side="left", padx=(SPACE_XS, 0))
        interval_combobox.bind("<<ComboboxSelected>>", lambda event: self._reschedule_auto_refresh())

        ttk.Label(
            action_bar,
            text="双击看详情 · 右键更多操作 · F5 刷新 · Delete 终止",
            style="Status.TLabel",
        ).pack(side="right")

        self._update_action_button_states()

    def _build_status_bar(self) -> None:
        """状态栏：左侧统计，右侧放主题切换。

        统计分两级——主计数（显示多少条、其中多少可终止）用正文色加粗，
        「系统共 N 条 · 最后刷新」这类背景信息用小字次要色。
        原先整条是同字号同颜色的一句话，扫不出重点。

        主题属于低频的环境设置，放在视觉层级最低的位置，
        不与筛选条件和操作按钮抢注意力。
        """
        ttk.Frame(self.root, style="Divider.TFrame", height=1).pack(fill="x")
        status_bar = ttk.Frame(self.root, padding=(SPACE_XL, SPACE_SM, SPACE_XL, SPACE_MD))
        status_bar.pack(fill="x")

        self.theme_button = ttk.Button(
            status_bar,
            text="暗色",
            style="Secondary.TButton",
            width=6,
            command=self.toggle_theme,
        )
        self.theme_button.pack(side="right")
        self._update_theme_button_label()

        ttk.Label(
            status_bar, textvariable=self.status_message, style="StatusStrong.TLabel"
        ).pack(side="left")
        ttk.Label(
            status_bar, textvariable=self.status_detail_message, style="Status.TLabel"
        ).pack(side="left", padx=(SPACE_MD, 0), pady=(SPACE_XS, 0))

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
        """关闭窗口前取消所有已排程回调，避免访问已销毁的控件。

        做成幂等的：快速连点关闭按钮、或程序内部已经关过一次时，
        重复调用不应该因为操作已销毁的控件而抛异常。
        """
        if self.is_closing:
            return
        self.is_closing = True

        for scheduled_id in (self.scheduled_auto_refresh_id, self.scheduled_poll_id):
            if scheduled_id is not None:
                try:
                    self.root.after_cancel(scheduled_id)
                except tk.TclError:
                    pass
        self.scheduled_auto_refresh_id = None
        self.scheduled_poll_id = None

        try:
            self.root.destroy()
        except tk.TclError:
            pass

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
        # 行 id 会被复用，旧的 hover 标签必须先失效，否则会残留在无关行上。
        self.hovered_row_id = None

        common_dev_ports = set(COMMON_DEV_PORTS)

        for row_index, binding in enumerate(self.displayed_bindings):
            disposition = classify_binding(binding)
            row_tags: list[str] = []

            if disposition == DISPOSITION_KERNEL:
                # 没有真实进程，显示进程名会让人以为可以去杀它。
                process_label = "内核残留连接"
                row_tags.append("kernel_placeholder")
            elif disposition == DISPOSITION_PROTECTED:
                process_label = binding.process_name
                row_tags.append("system_process")
            else:
                process_label = binding.process_name
                # 用户几乎总是在找开发端口，这类行不用筛选就该能被扫到；
                # 其余可终止行用斑马纹辅助横向阅读。
                if binding.local_port in common_dev_ports:
                    row_tags.append("dev_port")
                elif row_index % 2 == 1:
                    row_tags.append("stripe")

            self.table.insert(
                "",
                "end",
                iid=str(row_index),
                values=(
                    binding.local_port,
                    binding.protocol,
                    binding.state or "—",
                    binding.pid if binding.pid > 0 else "—",
                    process_label,
                    DISPOSITION_TABLE_LABELS[disposition],
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

        primary_parts = [f"显示 {len(self.displayed_bindings)} 条", f"可终止 {killable_count}"]
        if protected_count:
            primary_parts.append(f"系统保护 {protected_count}")
        if kernel_placeholder_count:
            primary_parts.append(f"内核残留 {kernel_placeholder_count}")
        self.status_message.set(" · ".join(primary_parts))

        detail_parts = [
            f"系统共 {len(self.all_bindings)} 条记录",
            f"最后刷新 {time.strftime('%H:%M:%S')}",
        ]
        self.status_detail_message.set(" · ".join(detail_parts))

    def _update_action_button_states(self) -> None:
        selected_bindings = self._get_selected_bindings()
        has_killable_selection = any(binding.pid > 0 for binding in selected_bindings)
        allow_actions = not self.is_task_running

        self.terminate_button.configure(
            state="normal" if allow_actions and has_killable_selection else "disabled"
        )

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
