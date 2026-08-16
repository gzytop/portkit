#!/usr/bin/env python3
"""生成 portkit 的应用图标（app_icon.ico）。

手写 ICO/BMP 字节，只依赖标准库，因此不需要 Pillow。
图标含义：蓝色圆底上的白色插座，配一道红色斜杠表示「切断占用」。

用法:
  python make_icon.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)

COLOR_TRANSPARENT = (0, 0, 0, 0)
COLOR_BADGE_BLUE = (37, 99, 235, 255)
COLOR_BADGE_BLUE_DARK = (29, 78, 216, 255)
COLOR_PORT_WHITE = (255, 255, 255, 255)
COLOR_CUT_RED = (220, 38, 38, 255)


def build_icon_pixels(size: int) -> list[list[tuple[int, int, int, int]]]:
    """按比例绘制图标像素，返回 [行][列] 的 RGBA 矩阵（行序为从上到下）。"""
    pixels = [[COLOR_TRANSPARENT for _ in range(size)] for _ in range(size)]

    center = (size - 1) / 2.0
    outer_radius = size * 0.47
    inner_radius = size * 0.40

    # 圆角矩形插座主体（白色）在圆形蓝底之上。
    socket_half_width = size * 0.24
    socket_half_height = size * 0.20
    socket_corner = max(1.0, size * 0.05)

    # 两个插孔（蓝色），模拟插座的接触点。
    hole_offset_x = size * 0.115
    hole_half_width = max(1.0, size * 0.045)
    hole_half_height = max(1.0, size * 0.095)

    for row in range(size):
        for column in range(size):
            distance_x = column - center
            distance_y = row - center
            distance_from_center = (distance_x * distance_x + distance_y * distance_y) ** 0.5

            if distance_from_center > outer_radius:
                continue

            # 圆形底盘：边缘用深一号的蓝色做一圈描边，视觉上更清晰。
            pixels[row][column] = (
                COLOR_BADGE_BLUE if distance_from_center <= inner_radius else COLOR_BADGE_BLUE_DARK
            )

            inside_socket_body = _is_inside_rounded_rectangle(
                distance_x, distance_y, socket_half_width, socket_half_height, socket_corner
            )
            if inside_socket_body:
                pixels[row][column] = COLOR_PORT_WHITE

                for hole_center_x in (-hole_offset_x, hole_offset_x):
                    if (
                        abs(distance_x - hole_center_x) <= hole_half_width
                        and abs(distance_y) <= hole_half_height
                    ):
                        pixels[row][column] = COLOR_BADGE_BLUE

            # 红色斜杠：表示「切断 / 终止占用」，从左下贯穿到右上。
            slash_distance = abs(distance_x + distance_y)
            slash_half_thickness = max(1.0, size * 0.055)
            if slash_distance <= slash_half_thickness and distance_from_center <= inner_radius:
                pixels[row][column] = COLOR_CUT_RED

    return pixels


def _is_inside_rounded_rectangle(
    offset_x: float, offset_y: float, half_width: float, half_height: float, corner_radius: float
) -> bool:
    absolute_x = abs(offset_x)
    absolute_y = abs(offset_y)
    if absolute_x > half_width or absolute_y > half_height:
        return False

    corner_center_x = half_width - corner_radius
    corner_center_y = half_height - corner_radius
    if absolute_x <= corner_center_x or absolute_y <= corner_center_y:
        return True

    corner_distance_x = absolute_x - corner_center_x
    corner_distance_y = absolute_y - corner_center_y
    return (corner_distance_x**2 + corner_distance_y**2) ** 0.5 <= corner_radius


def encode_bmp_with_alpha(pixels: list[list[tuple[int, int, int, int]]]) -> bytes:
    """把像素矩阵编码为 ICO 内嵌的 32 位 BMP（DIB，不含文件头）。

    BMP 的行序是自下而上，且 ICO 中的 BITMAPINFOHEADER 高度要写成实际高度的两倍
    （颜色数据 + AND 掩码），即使 32 位图不再需要单独的掩码数据。
    """
    height = len(pixels)
    width = len(pixels[0])

    header = struct.pack(
        "<IiiHHIIiiII",
        40,             # biSize
        width,          # biWidth
        height * 2,     # biHeight（颜色 + 掩码）
        1,              # biPlanes
        32,             # biBitCount
        0,              # biCompression = BI_RGB
        width * height * 4,
        0, 0, 0, 0,
    )

    color_rows = bytearray()
    for row in reversed(pixels):
        for red, green, blue, alpha in row:
            color_rows += bytes((blue, green, red, alpha))

    # AND 掩码：每行按 4 字节对齐，32 位图里全 0 即可。
    mask_row_size = ((width + 31) // 32) * 4
    and_mask = bytes(mask_row_size * height)

    return header + bytes(color_rows) + and_mask


def write_ico_file(destination: Path, sizes: tuple[int, ...] = ICON_SIZES) -> None:
    images = [(size, encode_bmp_with_alpha(build_icon_pixels(size))) for size in sizes]

    directory_entries = bytearray()
    image_payloads = bytearray()
    offset = 6 + 16 * len(images)

    for size, image_bytes in images:
        # ICO 目录里 256 用 0 表示。
        stored_size = 0 if size >= 256 else size
        directory_entries += struct.pack(
            "<BBBBHHII",
            stored_size,
            stored_size,
            0,      # 调色板数量（真彩色为 0）
            0,      # 保留位
            1,      # 色彩平面
            32,     # 位深
            len(image_bytes),
            offset,
        )
        image_payloads += image_bytes
        offset += len(image_bytes)

    file_header = struct.pack("<HHH", 0, 1, len(images))
    destination.write_bytes(file_header + bytes(directory_entries) + bytes(image_payloads))


def allow_non_ascii_output() -> None:
    """让中文提示在非 UTF-8 控制台上也不会让脚本崩掉。

    英文版 Windows（以及 GitHub Actions 的 Windows runner）默认用 cp1252，
    直接 print 中文会抛 UnicodeEncodeError，导致构建在「生成图标」这步失败。

    这里不复用 portkit.py 里的同类函数，是为了让本脚本保持独立可运行——
    生成图标不应该依赖端口工具模块。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            try:
                stream.reconfigure(errors="replace")
            except (AttributeError, ValueError):
                pass


def main() -> int:
    allow_non_ascii_output()
    destination = Path(__file__).with_name("app_icon.ico")
    write_ico_file(destination)
    print(f"已生成图标: {destination}  ({destination.stat().st_size} 字节, 尺寸 {ICON_SIZES})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
