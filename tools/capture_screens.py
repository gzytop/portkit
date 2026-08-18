#!/usr/bin/env python3
"""给两套主题各截一张界面图。

用途：改完 UI 后肉眼确认真实渲染效果。tkinter 的中文字体宽度、控件边框
与 DPI 缩放都会让实际画面偏离预期，只看代码判断不了。

用法:
  python tools/capture_screens.py <输出目录>

不进主程序依赖链，只在开发时手动跑。
"""

from __future__ import annotations

import sys
import time
import tkinter as tk
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import portkit_gui  # noqa: E402
from theme import DARK_PALETTE, LIGHT_PALETTE  # noqa: E402


def allow_non_ascii_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def capture_window(output_path: Path, use_dark_theme: bool) -> None:
    """开一个真实窗口、等数据加载完再截图。"""
    portkit_gui.enable_high_dpi_awareness()

    root = tk.Tk()
    palette = DARK_PALETTE if use_dark_theme else LIGHT_PALETTE
    application = portkit_gui.PortManagerApplication(root, palette=palette)
    try:
        root.geometry("1080x700")
        # 等真实端口数据到位，否则截到的是空表。
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            root.update()
            if application.all_bindings:
                break
            time.sleep(0.05)

        # 数据到位后再多刷几轮，让行着色和状态栏文本稳定下来。
        for _ in range(40):
            root.update()
            time.sleep(0.02)

        root.update_idletasks()
        _grab_window_image(root, output_path)
        print(f"已保存 {output_path}")
    finally:
        application.handle_window_close()


def _grab_window_image(root: tk.Tk, output_path: Path) -> None:
    """截取窗口区域。

    Windows 上用 PrintWindow 拿窗口自身的位图，避免被其他窗口遮挡影响，
    也不需要 Pillow（项目约定零第三方依赖）。
    """
    import ctypes
    from ctypes import wintypes

    window_handle = int(root.frame(), 16) if root.frame().startswith("0x") else int(root.frame())

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    rect = wintypes.RECT()
    user32.GetWindowRect(window_handle, ctypes.byref(rect))
    width = rect.right - rect.left
    height = rect.bottom - rect.top

    window_dc = user32.GetWindowDC(window_handle)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    gdi32.SelectObject(memory_dc, bitmap)

    # 2 = PW_RENDERFULLCONTENT，否则某些控件会截成空白。
    user32.PrintWindow(window_handle, memory_dc, 2)

    pixel_buffer = ctypes.create_string_buffer(width * height * 4)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = width
    # 负高度 = 自上而下的行序，省去后续翻转。
    header.biHeight = -height
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = 0

    gdi32.GetDIBits(memory_dc, bitmap, 0, height, pixel_buffer, ctypes.byref(header), 0)

    _write_png(output_path, width, height, pixel_buffer.raw)

    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(memory_dc)
    user32.ReleaseDC(window_handle, window_dc)


def _write_png(output_path: Path, width: int, height: int, bgra_pixels: bytes) -> None:
    """手写 PNG 编码（标准库 zlib + struct 足够，不需要 Pillow）。"""
    import struct
    import zlib

    raw_rows = bytearray()
    for row_index in range(height):
        raw_rows.append(0)  # 每行的过滤器类型：0 = None
        row_start = row_index * width * 4
        for column_index in range(width):
            pixel_start = row_start + column_index * 4
            blue = bgra_pixels[pixel_start]
            green = bgra_pixels[pixel_start + 1]
            red = bgra_pixels[pixel_start + 2]
            raw_rows.extend((red, green, blue))

    def build_chunk(chunk_type: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + chunk_type
            + payload
            + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)
        )

    header_payload = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + build_chunk(b"IHDR", header_payload)
        + build_chunk(b"IDAT", zlib.compress(bytes(raw_rows), 9))
        + build_chunk(b"IEND", b"")
    )
    output_path.write_bytes(png_bytes)


def main() -> int:
    allow_non_ascii_output()
    output_directory = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    output_directory.mkdir(parents=True, exist_ok=True)

    capture_window(output_directory / "screenshot.png", use_dark_theme=False)
    capture_window(output_directory / "screenshot-dark.png", use_dark_theme=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
