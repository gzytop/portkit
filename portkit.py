#!/usr/bin/env python3
"""portkit - 查看端口占用并终止占用进程的小工具。

零第三方依赖，只使用标准库 + 系统自带命令：
  * Windows: netstat -ano / tasklist / taskkill
  * macOS/Linux: lsof（首选）或 ss（备选）+ SIGTERM/SIGKILL

用法示例:
  python portkit.py                 # 交互模式（列出监听端口，选序号终止）
  python portkit.py ls              # 列出所有处于监听/绑定状态的端口
  python portkit.py check 3000 8080 # 查询指定端口被谁占用
  python portkit.py kill 3000       # 终止占用 3000 端口的进程（默认需确认）
  python portkit.py dev             # 扫描常见开发端口
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from typing import Iterable, Sequence

IS_WINDOWS = os.name == "nt"

# 这些进程通常由操作系统托管，误杀会导致系统不稳定甚至蓝屏/重启，
# 因此除非显式传入 --force 才允许终止。
PROTECTED_PROCESS_NAMES = {
    "system",
    "system idle process",
    "idle",
    "registry",
    "smss.exe",
    "csrss.exe",
    "wininit.exe",
    "winlogon.exe",
    "services.exe",
    "lsass.exe",
    "svchost.exe",
    "ntoskrnl.exe",
    "memory compression",
    "launchd",
    "systemd",
    "kernel_task",
    "init",
}
PROTECTED_PIDS = {0, 4}

# 常见的开发 / 中间件端口，用于 `dev` 子命令快速体检。
COMMON_DEV_PORTS = [
    80, 443, 3000, 3001, 3306, 4000, 4200, 5000, 5001, 5173, 5174,
    5432, 5672, 6379, 7860, 8000, 8080, 8081, 8888, 9000, 9090,
    9229, 11434, 15672, 27017,
]

LISTENING_STATES = {"LISTENING", "LISTEN", "UNCONN", ""}

# 在无控制台的窗口程序（pythonw / PyInstaller --noconsole）里调用 netstat 等控制台命令时，
# 子进程会自己弹一个黑窗口。CREATE_NO_WINDOW 可以阻止这个闪窗。
WINDOWS_CREATE_NO_WINDOW = 0x08000000
SUBPROCESS_NO_WINDOW_FLAGS = WINDOWS_CREATE_NO_WINDOW if IS_WINDOWS else 0


# --------------------------------------------------------------------------
# 终端着色
# --------------------------------------------------------------------------
class Palette:
    """极简 ANSI 颜色封装，不支持颜色时自动降级为空串。"""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)


def build_palette(force_no_color: bool) -> Palette:
    if force_no_color or os.environ.get("NO_COLOR"):
        return Palette(False)
    if not sys.stdout.isatty():
        return Palette(False)
    if IS_WINDOWS and not _enable_windows_virtual_terminal():
        return Palette(False)
    return Palette(True)


def _enable_windows_virtual_terminal() -> bool:
    """在旧版 Windows 控制台上开启 ANSI 转义支持。"""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        stdout_handle = kernel32.GetStdHandle(-11)
        current_mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(stdout_handle, ctypes.byref(current_mode)):
            return False
        enable_virtual_terminal_processing = 0x0004
        return bool(
            kernel32.SetConsoleMode(
                stdout_handle, current_mode.value | enable_virtual_terminal_processing
            )
        )
    except Exception:
        return False


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------
@dataclass
class PortBinding:
    """一条「某进程占用某端口」的记录。"""

    protocol: str
    local_address: str
    local_port: int
    state: str
    pid: int
    process_name: str

    @property
    def is_listening(self) -> bool:
        return self.state.upper() in LISTENING_STATES

    @property
    def is_protected(self) -> bool:
        return self.pid in PROTECTED_PIDS or self.process_name.lower() in PROTECTED_PROCESS_NAMES


class PortToolError(RuntimeError):
    """工具自身可预期的错误，用于输出友好提示而非堆栈。"""


# --------------------------------------------------------------------------
# 系统命令调用
# --------------------------------------------------------------------------
def run_system_command(command: Sequence[str]) -> tuple[int, str]:
    """执行系统命令并返回 (退出码, 解码后的输出)。"""
    try:
        completed = subprocess.run(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            creationflags=SUBPROCESS_NO_WINDOW_FLAGS,
        )
    except FileNotFoundError as error:
        raise PortToolError(f"找不到命令 {command[0]}：{error}") from error

    return completed.returncode, decode_console_bytes(completed.stdout)


def decode_console_bytes(raw_output: bytes) -> str:
    """按控制台常见编码依次尝试解码（中文 Windows 通常是 GBK/CP936）。"""
    candidate_encodings = ["utf-8", sys.getdefaultencoding()]
    if IS_WINDOWS:
        candidate_encodings.insert(0, "mbcs")
    for encoding in candidate_encodings:
        try:
            return raw_output.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw_output.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# 采集端口占用信息
# --------------------------------------------------------------------------
def collect_port_bindings() -> list[PortBinding]:
    """采集当前机器上全部 TCP/UDP 端口占用记录。"""
    if IS_WINDOWS:
        return _collect_bindings_windows()
    return _collect_bindings_posix()


def _collect_bindings_windows() -> list[PortBinding]:
    exit_code, output = run_system_command(["netstat", "-ano"])
    if exit_code != 0:
        raise PortToolError(f"netstat 执行失败（退出码 {exit_code}）：\n{output.strip()}")

    process_names_by_pid = _load_windows_process_names()
    bindings: list[PortBinding] = []

    for raw_line in output.splitlines():
        columns = raw_line.split()
        if len(columns) < 4 or columns[0].upper() not in {"TCP", "UDP"}:
            continue

        protocol = columns[0].upper()
        if protocol == "TCP" and len(columns) >= 5:
            state, pid_text = columns[3], columns[4]
        elif protocol == "UDP":
            # UDP 无连接状态列，最后一列即 PID。
            state, pid_text = "", columns[-1]
        else:
            continue

        if not pid_text.isdigit():
            continue

        address, port = split_address_and_port(columns[1])
        if port is None:
            continue

        pid = int(pid_text)
        bindings.append(
            PortBinding(
                protocol=protocol,
                local_address=address,
                local_port=port,
                state=state,
                pid=pid,
                process_name=process_names_by_pid.get(pid, "?"),
            )
        )

    return bindings


def _load_windows_process_names() -> dict[int, str]:
    """一次性拉取 PID -> 进程名 映射，避免逐个 PID 调用 tasklist。"""
    exit_code, output = run_system_command(["tasklist", "/FO", "CSV", "/NH"])
    if exit_code != 0:
        return {}

    names_by_pid: dict[int, str] = {}
    for row in csv.reader(io.StringIO(output)):
        if len(row) >= 2 and row[1].strip().isdigit():
            names_by_pid[int(row[1].strip())] = row[0].strip()
    return names_by_pid


def _collect_bindings_posix() -> list[PortBinding]:
    if shutil.which("lsof"):
        return _collect_bindings_with_lsof()
    if shutil.which("ss"):
        return _collect_bindings_with_ss()
    raise PortToolError("未找到 lsof 或 ss 命令，无法读取端口占用信息。")


def _collect_bindings_with_lsof() -> list[PortBinding]:
    # -nP 关闭 DNS / 服务名反查，输出更快且更易解析。
    exit_code, output = run_system_command(["lsof", "-nP", "-i"])
    # lsof 在部分条目无权限时会返回 1，但仍有可用输出，因此只在无输出时报错。
    if exit_code != 0 and not output.strip():
        raise PortToolError(f"lsof 执行失败（退出码 {exit_code}）")

    bindings: list[PortBinding] = []
    for raw_line in output.splitlines()[1:]:
        columns = raw_line.split()
        if len(columns) < 9:
            continue

        process_name, pid_text = columns[0], columns[1]
        if not pid_text.isdigit():
            continue

        protocol = columns[7].upper()
        if protocol not in {"TCP", "UDP"}:
            continue

        name_field = columns[8]
        state_match = re.search(r"\(([A-Z]+)\)", raw_line)
        state = state_match.group(1) if state_match else ""
        if "->" in name_field:
            name_field = name_field.split("->", 1)[0]

        address, port = split_address_and_port(name_field)
        if port is None:
            continue

        bindings.append(
            PortBinding(
                protocol=protocol,
                local_address=address,
                local_port=port,
                state=state,
                pid=int(pid_text),
                process_name=process_name,
            )
        )
    return bindings


def _collect_bindings_with_ss() -> list[PortBinding]:
    exit_code, output = run_system_command(["ss", "-tulnpH"])
    if exit_code != 0:
        raise PortToolError(f"ss 执行失败（退出码 {exit_code}）：\n{output.strip()}")

    bindings: list[PortBinding] = []
    for raw_line in output.splitlines():
        columns = raw_line.split()
        if len(columns) < 5:
            continue

        protocol = columns[0].upper()
        state = columns[1].upper()
        address, port = split_address_and_port(columns[4])
        if port is None:
            continue

        process_match = re.search(r'users:\(\("([^"]+)",pid=(\d+)', raw_line)
        process_name = process_match.group(1) if process_match else "?"
        pid = int(process_match.group(2)) if process_match else -1

        bindings.append(
            PortBinding(
                protocol=protocol,
                local_address=address,
                local_port=port,
                state=state,
                pid=pid,
                process_name=process_name,
            )
        )
    return bindings


def split_address_and_port(endpoint: str) -> tuple[str, int | None]:
    """拆分 `0.0.0.0:8080` / `[::]:8080` / `*:8080` 形式的地址。"""
    if ":" not in endpoint:
        return endpoint, None
    address, _, port_text = endpoint.rpartition(":")
    if not port_text.isdigit():
        return address, None
    return address or "*", int(port_text)


# --------------------------------------------------------------------------
# 过滤与展示
# --------------------------------------------------------------------------
def filter_bindings(
    bindings: Iterable[PortBinding],
    ports: Sequence[int] | None = None,
    protocol: str | None = None,
    listening_only: bool = True,
    name_keyword: str | None = None,
) -> list[PortBinding]:
    selected = list(bindings)
    if ports:
        wanted_ports = set(ports)
        selected = [item for item in selected if item.local_port in wanted_ports]
    if protocol:
        selected = [item for item in selected if item.protocol == protocol.upper()]
    if listening_only:
        selected = [item for item in selected if item.is_listening]
    if name_keyword:
        keyword = name_keyword.lower()
        selected = [item for item in selected if keyword in item.process_name.lower()]

    selected.sort(key=lambda item: (item.local_port, item.protocol, item.pid))
    return deduplicate_bindings(selected)


def deduplicate_bindings(bindings: Sequence[PortBinding]) -> list[PortBinding]:
    """同一个 PID 在 IPv4/IPv6 上重复监听同一端口时只保留一条。"""
    unique: dict[tuple[str, int, int], PortBinding] = {}
    for binding in bindings:
        key = (binding.protocol, binding.local_port, binding.pid)
        if key not in unique:
            unique[key] = binding
    return list(unique.values())


def render_bindings_table(
    bindings: Sequence[PortBinding], palette: Palette, show_index: bool = False
) -> str:
    if not bindings:
        return palette.dim("（无匹配记录）")

    headers = ["#", "端口", "协议", "状态", "PID", "进程", "监听地址"]
    rows: list[list[str]] = []
    for index, binding in enumerate(bindings, start=1):
        if binding.pid <= 0:
            # PID 0 是内核占位（TIME_WAIT 等待关闭的连接），显示真实进程名会误导。
            process_label = "内核残留连接"
        elif binding.is_protected:
            process_label = f"{binding.process_name} [系统]"
        else:
            process_label = binding.process_name
        rows.append(
            [
                str(index) if show_index else "",
                str(binding.local_port),
                binding.protocol,
                binding.state or "-",
                str(binding.pid),
                process_label,
                binding.local_address,
            ]
        )

    if not show_index:
        headers = headers[1:]
        rows = [row[1:] for row in rows]

    column_widths = [
        max(display_width(header), *(display_width(row[column]) for row in rows))
        for column, header in enumerate(headers)
    ]

    lines = [
        palette.bold("  ".join(pad_to_width(header, column_widths[i]) for i, header in enumerate(headers))),
        palette.dim("  ".join("-" * width for width in column_widths)),
    ]
    for row, binding in zip(rows, bindings):
        line = "  ".join(pad_to_width(cell, column_widths[i]) for i, cell in enumerate(row))
        lines.append(palette.yellow(line) if binding.is_protected else line)
    return "\n".join(lines)


def display_width(text: str) -> int:
    """按东亚宽字符占 2 列估算显示宽度，保证表格对齐。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def pad_to_width(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


# --------------------------------------------------------------------------
# 进程终止
# --------------------------------------------------------------------------
def is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        exit_code, output = run_system_command(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]
        )
        return exit_code == 0 and f'"{pid}"' in output
    return _is_posix_process_alive(pid)


def _is_posix_process_alive(pid: int) -> bool:
    """判断 POSIX 进程是否真的还在运行。

    不能只靠 `os.kill(pid, 0)`：进程被终止后会变成僵尸（zombie），
    也就是「已经退出、但父进程还没回收」的进程表条目，此时 kill(pid, 0)
    依然成功。若把僵尸当成存活，终止操作会一直等到超时并误报「进程仍在运行」，
    让用户以为没杀掉。
    """
    _reap_if_own_child(pid)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 属于其他用户，只能确认它存在；这种进程不会是本进程要回收的僵尸。
        return True

    return not _is_zombie_process(pid)


def _reap_if_own_child(pid: int) -> None:
    """若 pid 是本进程的子进程，顺手回收，避免留下僵尸条目。

    对不是自己子进程的 pid，waitpid 会抛 ChildProcessError，忽略即可。
    """
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _is_zombie_process(pid: int) -> bool:
    """检测进程是否处于僵尸状态。"""
    linux_stat_path = f"/proc/{pid}/stat"
    if os.path.exists(linux_stat_path):
        try:
            with open(linux_stat_path, "r", encoding="utf-8", errors="replace") as stat_file:
                stat_content = stat_file.read()
        except OSError:
            return False
        # 格式为 `pid (进程名) 状态 ...`，而进程名自身可能含空格和括号，
        # 因此从最后一个 ')' 之后开始取状态字段。
        _, _, fields_after_name = stat_content.rpartition(")")
        state_fields = fields_after_name.split()
        return bool(state_fields) and state_fields[0] == "Z"

    # macOS 等没有 /proc 的系统退回 ps 查询进程状态。
    exit_code, output = run_system_command(["ps", "-o", "state=", "-p", str(pid)])
    if exit_code != 0:
        return False
    return output.strip().upper().startswith("Z")


def terminate_process(pid: int, force: bool = False, include_children: bool = False) -> tuple[bool, str]:
    """终止进程：先温和请求退出，超时后强制结束。返回 (是否成功, 说明)。"""
    if not is_process_running(pid):
        return True, "进程已不存在"

    if not force:
        _request_graceful_exit(pid, include_children)
        if _wait_until_process_exits(pid, timeout_seconds=2.0):
            return True, "已优雅退出"

    return _force_kill_process(pid, include_children)


def _request_graceful_exit(pid: int, include_children: bool) -> None:
    if IS_WINDOWS:
        command = ["taskkill", "/PID", str(pid)]
        if include_children:
            command.append("/T")
        run_system_command(command)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def _force_kill_process(pid: int, include_children: bool) -> tuple[bool, str]:
    if IS_WINDOWS:
        command = ["taskkill", "/F", "/PID", str(pid)]
        if include_children:
            command.append("/T")
        exit_code, output = run_system_command(command)
        if exit_code == 0 or not is_process_running(pid):
            return True, "已强制结束"
        hint = output.strip().splitlines()[-1] if output.strip() else f"退出码 {exit_code}"
        if "拒绝访问" in hint or "Access is denied" in hint:
            hint += "（请用管理员身份重新运行）"
        return False, hint

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True, "进程已不存在"
    except PermissionError:
        return False, "权限不足（尝试 sudo）"

    if _wait_until_process_exits(pid, timeout_seconds=2.0):
        return True, "已强制结束"
    return False, "进程仍在运行"


def _wait_until_process_exits(pid: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not is_process_running(pid):
            return True
        time.sleep(0.15)
    return not is_process_running(pid)


def wait_until_port_released(port: int, timeout_seconds: float = 3.0) -> bool:
    """等待端口真正空出来；TIME_WAIT 等无主残留连接不算占用。"""
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = [
            binding
            for binding in filter_bindings(collect_port_bindings(), ports=[port], listening_only=False)
            if binding.pid > 0
        ]
        if not remaining:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


# --------------------------------------------------------------------------
# 子命令实现
# --------------------------------------------------------------------------
def command_list(arguments: argparse.Namespace, palette: Palette) -> int:
    bindings = filter_bindings(
        collect_port_bindings(),
        ports=arguments.port,
        protocol=arguments.protocol,
        listening_only=not arguments.all,
        name_keyword=arguments.name,
    )

    if arguments.json:
        print(json.dumps([asdict(item) for item in bindings], ensure_ascii=False, indent=2))
        return 0

    print(render_bindings_table(bindings, palette))
    print(palette.dim(f"\n共 {len(bindings)} 条记录"))
    return 0


def get_invocation_name() -> str:
    """返回当前该怎么调用本工具，用于提示语。

    打包成 exe 后没有 python 解释器和 .py 文件，提示「python portkit.py ...」会误导用户。
    """
    if getattr(sys, "frozen", False):
        return os.path.basename(sys.executable)
    return f"python {os.path.basename(__file__)}"


def command_check(arguments: argparse.Namespace, palette: Palette) -> int:
    all_bindings = collect_port_bindings()
    occupied_any = False
    results: list[dict] = []

    for port in arguments.ports:
        matched = filter_bindings(all_bindings, ports=[port], listening_only=False)
        # PID 为 0 的条目是内核维护的残留连接（如 TIME_WAIT），没有进程可杀。
        owner_bindings = [item for item in matched if item.pid > 0]
        results.append(
            {
                "port": port,
                "occupied": bool(owner_bindings),
                "bindings": [asdict(item) for item in matched],
            }
        )

        if not matched:
            print(f"{palette.green('空闲')}  端口 {palette.bold(str(port))} 未被占用")
            continue

        if not owner_bindings:
            print(
                f"{palette.yellow('残留')}  端口 {palette.bold(str(port))} 只有 TIME_WAIT 等待关闭的连接，"
                "无进程占用，通常几十秒后自动释放"
            )
            print(render_bindings_table(matched, palette))
            continue

        occupied_any = True
        owners = "、".join(sorted({f"{item.process_name}(PID {item.pid})" for item in owner_bindings}))
        print(f"{palette.red('占用')}  端口 {palette.bold(str(port))} 被 {owners} 占用")
        print(render_bindings_table(matched, palette))
        print(palette.dim(f"       释放命令: {get_invocation_name()} kill {port}"))

    if arguments.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))

    # 端口被进程占用时返回 1，方便在脚本里做条件判断。
    return 1 if occupied_any else 0


def command_kill(arguments: argparse.Namespace, palette: Palette) -> int:
    exit_code = 0
    for port in arguments.ports:
        if not _kill_processes_on_port(
            port,
            palette,
            assume_yes=arguments.yes,
            force=arguments.force,
            include_children=arguments.tree,
        ):
            exit_code = 1
    return exit_code


def _kill_processes_on_port(
    port: int,
    palette: Palette,
    assume_yes: bool,
    force: bool,
    include_children: bool,
) -> bool:
    matched = filter_bindings(collect_port_bindings(), ports=[port], listening_only=False)
    if not matched:
        print(f"{palette.green('跳过')}  端口 {port} 未被占用")
        return True

    print(f"端口 {palette.bold(str(port))} 的占用情况：")
    print(render_bindings_table(matched, palette))

    killable_pids = sorted({binding.pid for binding in matched if binding.pid > 0})
    if not killable_pids:
        print(
            palette.yellow(
                f"注意  端口 {port} 上只有 TIME_WAIT 等无主连接，没有可终止的进程，稍等即会自动释放"
            )
        )
        return True

    all_succeeded = True
    for pid in killable_pids:
        binding = next(item for item in matched if item.pid == pid)

        if binding.is_protected and not force:
            print(
                palette.yellow(
                    f"警告  PID {pid}（{binding.process_name}）属于系统关键进程，已跳过。"
                    " 确认要终止请加 --force。"
                )
            )
            all_succeeded = False
            continue

        if not assume_yes and not confirm_action(
            f"终止 PID {pid}（{binding.process_name}）以释放端口 {port}？"
        ):
            print(palette.dim(f"      已取消 PID {pid}"))
            all_succeeded = False
            continue

        succeeded, detail = terminate_process(pid, force=force, include_children=include_children)
        if succeeded:
            print(f"{palette.green('成功')}  PID {pid}（{binding.process_name}）{detail}")
        else:
            print(f"{palette.red('失败')}  PID {pid}（{binding.process_name}）：{detail}")
            all_succeeded = False

    if all_succeeded:
        if wait_until_port_released(port):
            print(f"{palette.green('完成')}  端口 {port} 已释放")
        else:
            print(palette.yellow(f"注意  端口 {port} 仍显示被占用，可能处于 TIME_WAIT 或有残留进程"))
            all_succeeded = False
    return all_succeeded


def command_dev(arguments: argparse.Namespace, palette: Palette) -> int:
    ports_to_scan = arguments.port or COMMON_DEV_PORTS
    bindings = filter_bindings(collect_port_bindings(), ports=ports_to_scan, listening_only=False)

    occupied_ports = sorted({binding.local_port for binding in bindings})
    free_ports = [port for port in sorted(set(ports_to_scan)) if port not in occupied_ports]

    print(palette.bold("常见开发端口体检"))
    print(render_bindings_table(bindings, palette))
    print(palette.green(f"\n空闲端口（{len(free_ports)}）: ") + ", ".join(map(str, free_ports)))
    if occupied_ports:
        print(palette.red(f"占用端口（{len(occupied_ports)}）: ") + ", ".join(map(str, occupied_ports)))
    return 0


def command_interactive(palette: Palette) -> int:
    """交互模式：列表 + 序号选择终止，适合日常「端口被占用」救火。"""
    hide_system_processes = True

    while True:
        all_listening = filter_bindings(collect_port_bindings(), listening_only=True)
        visible = (
            [binding for binding in all_listening if not binding.is_protected]
            if hide_system_processes
            else all_listening
        )

        hidden_count = len(all_listening) - len(visible)
        title = "当前监听端口" + ("（已隐藏系统进程）" if hide_system_processes else "（含系统进程）")
        print()
        print(palette.bold(title))
        print(render_bindings_table(visible, palette, show_index=True))
        if hidden_count:
            print(palette.dim(f"（已隐藏 {hidden_count} 条系统进程记录，输入 a 显示全部）"))
        print(
            palette.dim(
                "\n输入序号终止对应进程；:<端口号> 按端口终止；a=切换系统进程显示；r=刷新；q=退出"
            )
        )

        try:
            user_input = input("portkit> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if user_input in {"q", "quit", "exit"}:
            return 0
        if user_input in {"", "r", "refresh"}:
            continue
        if user_input in {"a", "all"}:
            hide_system_processes = not hide_system_processes
            continue

        if user_input.startswith(":"):
            port_text = user_input[1:].strip()
            if not port_text.isdigit():
                print(palette.red("请输入合法端口号，例如 :3000"))
                continue
            _kill_processes_on_port(
                int(port_text), palette, assume_yes=False, force=False, include_children=False
            )
            continue

        if user_input.isdigit() and 1 <= int(user_input) <= len(visible):
            selected = visible[int(user_input) - 1]
            _kill_processes_on_port(
                selected.local_port, palette, assume_yes=False, force=False, include_children=False
            )
            continue

        print(palette.red("无法识别的输入。"))


def confirm_action(question: str) -> bool:
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in {"y", "yes"}


# --------------------------------------------------------------------------
# 命令行入口
# --------------------------------------------------------------------------
def parse_port_argument(value: str) -> int:
    if not value.isdigit() or not (0 < int(value) <= 65535):
        raise argparse.ArgumentTypeError(f"{value} 不是合法端口号（1-65535）")
    return int(value)


def configure_stdout_encoding() -> None:
    """让中文输出在非 UTF-8 控制台（如英文版 Windows 的 cp1252）也不崩。

    优先切到 UTF-8 以保留中文；不支持时退回 errors="replace"，
    宁可显示成问号也不要因为 UnicodeEncodeError 直接终止。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            try:
                stream.reconfigure(errors="replace")
            except (AttributeError, ValueError):
                pass


def build_argument_parser() -> argparse.ArgumentParser:
    # 放进 parents 里，使 --no-color 在子命令前后都能书写。
    shared_options_parser = argparse.ArgumentParser(add_help=False)
    shared_options_parser.add_argument("--no-color", action="store_true", help="禁用彩色输出")

    parser = argparse.ArgumentParser(
        prog="portkit",
        description="查看端口占用并终止占用进程；不带子命令时进入交互模式。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[shared_options_parser],
        epilog=(
            "示例:\n"
            "  portkit ls --protocol tcp\n"
            "  portkit check 3000 5173\n"
            "  portkit kill 3000 -y\n"
            "  portkit dev\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser(
        "ls", aliases=["list"], parents=[shared_options_parser], help="列出端口占用"
    )
    list_parser.add_argument("--port", type=parse_port_argument, nargs="*", help="只看指定端口")
    list_parser.add_argument("--protocol", choices=["tcp", "udp", "TCP", "UDP"], help="按协议过滤")
    list_parser.add_argument("--name", help="按进程名关键字过滤")
    list_parser.add_argument("-a", "--all", action="store_true", help="包含非监听连接（如 ESTABLISHED）")
    list_parser.add_argument("--json", action="store_true", help="以 JSON 输出")

    check_parser = subparsers.add_parser(
        "check", parents=[shared_options_parser], help="检查指定端口是否被占用"
    )
    check_parser.add_argument("ports", type=parse_port_argument, nargs="+")
    check_parser.add_argument("--json", action="store_true", help="附加 JSON 输出")

    kill_parser = subparsers.add_parser(
        "kill", aliases=["free"], parents=[shared_options_parser], help="终止占用指定端口的进程"
    )
    kill_parser.add_argument("ports", type=parse_port_argument, nargs="+")
    kill_parser.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    kill_parser.add_argument("-f", "--force", action="store_true", help="直接强杀，并允许终止系统进程")
    kill_parser.add_argument("-t", "--tree", action="store_true", help="连带终止子进程（Windows: taskkill /T）")

    dev_parser = subparsers.add_parser(
        "dev", parents=[shared_options_parser], help="扫描常见开发端口"
    )
    dev_parser.add_argument("--port", type=parse_port_argument, nargs="*", help="自定义待扫描端口")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdout_encoding()
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)
    palette = build_palette(arguments.no_color)

    try:
        if arguments.command in {"ls", "list"}:
            return command_list(arguments, palette)
        if arguments.command == "check":
            return command_check(arguments, palette)
        if arguments.command in {"kill", "free"}:
            return command_kill(arguments, palette)
        if arguments.command == "dev":
            return command_dev(arguments, palette)
        return command_interactive(palette)
    except PortToolError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
