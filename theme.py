#!/usr/bin/env python3
"""portkit 的设计令牌：色板、字号阶梯、间距标尺。

为什么要自己算色彩空间
----------------------
tkinter 只接受 `#rrggbb`，没有 CSS 那样的 `oklch()`。而直接手写十六进制色阶
很难做到感知均匀——HSL 里亮度相同的两个色，人眼看起来可能一深一浅。

所以这里实现 OKLCH → sRGB 的转换，色板用「亮度 / 彩度 / 色相」三个可解释的
维度描述，再编译成 tkinter 能吃的十六进制。好处：

* 中性灰统一向品牌色相偏移一点点彩度（tinted neutrals），整体更协调，
  又不会让人明确看出"这灰是蓝的"；
* 明暗两套主题可以用同一套语义、不同亮度值推导，避免两边各写一遍魔法值；
* 对比度可以直接算出来并断言，而不是靠肉眼判断（见 tests/smoke_test.py）。

只依赖标准库 math，符合项目的零第三方依赖约定。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# 品牌色相（OKLCH 的 H，单位为度）。245 是偏冷的钢蓝，
# 用于主操作、焦点与「这是你要找的开发端口」这类强调。
BRAND_HUE = 245.0
# 破坏性操作用的红，以及系统进程警示用的琥珀。
DANGER_HUE = 27.0
CAUTION_HUE = 70.0

# 中性色带一点点品牌彩度即可，超过 0.02 就会显出明显色偏。
NEUTRAL_CHROMA = 0.006


# --------------------------------------------------------------------------
# OKLCH → sRGB
# --------------------------------------------------------------------------
def oklch_to_hex(lightness: float, chroma: float, hue_degrees: float) -> str:
    """把 OKLCH 颜色转成 tkinter 可用的 `#rrggbb`。

    lightness 取 0..1，chroma 通常 0..0.4，hue_degrees 为 0..360。
    超出 sRGB 色域的颜色会被逐通道钳制——对本项目用到的低彩度色够用，
    不需要引入完整的色域映射。
    """
    hue_radians = math.radians(hue_degrees)
    opponent_a = chroma * math.cos(hue_radians)
    opponent_b = chroma * math.sin(hue_radians)

    long_root = lightness + 0.3963377774 * opponent_a + 0.2158037573 * opponent_b
    medium_root = lightness - 0.1055613458 * opponent_a - 0.0638541728 * opponent_b
    short_root = lightness - 0.0894841775 * opponent_a - 1.2914855480 * opponent_b

    long_cone = long_root**3
    medium_cone = medium_root**3
    short_cone = short_root**3

    linear_red = 4.0767416621 * long_cone - 3.3077115913 * medium_cone + 0.2309699292 * short_cone
    linear_green = -1.2684380046 * long_cone + 2.6097574011 * medium_cone - 0.3413193965 * short_cone
    linear_blue = -0.0041960863 * long_cone - 0.7034186147 * medium_cone + 1.7076147010 * short_cone

    channels = (
        _encode_srgb_gamma(linear_red),
        _encode_srgb_gamma(linear_green),
        _encode_srgb_gamma(linear_blue),
    )
    return "#" + "".join(f"{round(channel * 255):02x}" for channel in channels)


def _encode_srgb_gamma(linear_value: float) -> float:
    """线性光强 → sRGB 编码值，并钳制到 [0, 1]。"""
    clamped = min(max(linear_value, 0.0), 1.0)
    if clamped <= 0.0031308:
        return clamped * 12.92
    return 1.055 * (clamped ** (1 / 2.4)) - 0.055


def _decode_srgb_gamma(encoded_value: float) -> float:
    if encoded_value <= 0.04045:
        return encoded_value / 12.92
    return ((encoded_value + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color: str) -> float:
    """WCAG 定义的相对亮度，用于计算对比度。"""
    hex_digits = hex_color.lstrip("#")
    channels = [int(hex_digits[index : index + 2], 16) / 255 for index in (0, 2, 4)]
    red, green, blue = (_decode_srgb_gamma(channel) for channel in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(foreground_hex: str, background_hex: str) -> float:
    """WCAG 对比度（1..21）。正文需要 >= 4.5，大字号需要 >= 3。"""
    foreground_luminance = relative_luminance(foreground_hex)
    background_luminance = relative_luminance(background_hex)
    lighter = max(foreground_luminance, background_luminance)
    darker = min(foreground_luminance, background_luminance)
    return (lighter + 0.05) / (darker + 0.05)


# --------------------------------------------------------------------------
# 色板
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Palette:
    """一套主题的全部颜色。

    字段按「用途」命名而不是按颜色命名——`accent` 而不是 `blue`，
    这样换色相时不需要改调用处。
    """

    name: str
    is_dark: bool

    window: str            # 窗口底色
    surface: str           # 表格等内容区底色
    surface_raised: str    # 工具条 / 状态条，与窗口略作区分
    row_stripe: str        # 表格斑马纹
    border: str            # 分隔线与描边

    text_primary: str      # 主要文字
    text_secondary: str    # 次要说明文字
    text_disabled: str     # 禁用态

    accent: str            # 主色：主操作、焦点、开发端口强调
    accent_hover: str
    accent_text: str       # 主色块上的文字
    accent_soft: str       # 主色的浅色底（选中行、开发端口行背景）

    danger: str            # 破坏性操作
    danger_hover: str
    danger_text: str

    caution: str           # 系统受保护进程
    caution_soft: str      # 其行背景

    selection: str         # 表格选中行背景
    selection_text: str


def _build_palette(name: str, is_dark: bool) -> Palette:
    """从少量亮度锚点推导整套颜色。

    明暗两套主题共用同一份语义定义，只是亮度方向相反：
    亮色里"更高层级"意味着更亮，暗色里意味着更暗一点点再配更亮的文字。
    """
    neutral = lambda lightness: oklch_to_hex(lightness, NEUTRAL_CHROMA, BRAND_HUE)  # noqa: E731

    if not is_dark:
        return Palette(
            name=name,
            is_dark=False,
            window=neutral(0.968),
            surface=oklch_to_hex(0.995, 0.002, BRAND_HUE),
            surface_raised=neutral(0.985),
            row_stripe=neutral(0.972),
            border=neutral(0.886),
            text_primary=neutral(0.268),
            text_secondary=neutral(0.520),
            text_disabled=neutral(0.680),
            accent=oklch_to_hex(0.520, 0.170, BRAND_HUE),
            accent_hover=oklch_to_hex(0.455, 0.175, BRAND_HUE),
            accent_text="#ffffff",
            # 开发端口行的底色。刻意压到很淡：它的职责是「让这几行能被扫到」，
            # 不是喊出来，正文仍要保持最高可读性。
            accent_soft=oklch_to_hex(0.958, 0.028, BRAND_HUE),
            danger=oklch_to_hex(0.535, 0.198, DANGER_HUE),
            danger_hover=oklch_to_hex(0.468, 0.196, DANGER_HUE),
            danger_text="#ffffff",
            caution=oklch_to_hex(0.470, 0.115, CAUTION_HUE),
            caution_soft=oklch_to_hex(0.958, 0.040, CAUTION_HUE),
            selection=oklch_to_hex(0.520, 0.170, BRAND_HUE),
            selection_text="#ffffff",
        )

    return Palette(
        name=name,
        is_dark=True,
        window=neutral(0.198),
        surface=neutral(0.244),
        surface_raised=neutral(0.222),
        row_stripe=neutral(0.272),
        border=neutral(0.364),
        text_primary=neutral(0.936),
        text_secondary=neutral(0.722),
        text_disabled=neutral(0.520),
        # 暗背景上必须显著提亮主色，否则深蓝会糊成一团。
        accent=oklch_to_hex(0.720, 0.145, BRAND_HUE),
        accent_hover=oklch_to_hex(0.790, 0.135, BRAND_HUE),
        accent_text=neutral(0.180),
        accent_soft=oklch_to_hex(0.320, 0.070, BRAND_HUE),
        danger=oklch_to_hex(0.660, 0.185, DANGER_HUE),
        danger_hover=oklch_to_hex(0.720, 0.170, DANGER_HUE),
        danger_text=neutral(0.170),
        caution=oklch_to_hex(0.800, 0.130, CAUTION_HUE),
        caution_soft=oklch_to_hex(0.320, 0.055, CAUTION_HUE),
        # 选中底不能一味调亮：亮度越高，白字对比度越低。
        # 0.50 让白字拿到约 6:1 的余量，同时在暗色表格里仍足够醒目。
        selection=oklch_to_hex(0.500, 0.155, BRAND_HUE),
        selection_text="#ffffff",
    )


LIGHT_PALETTE = _build_palette("light", is_dark=False)
DARK_PALETTE = _build_palette("dark", is_dark=True)
PALETTES = {"light": LIGHT_PALETTE, "dark": DARK_PALETTE}


# --------------------------------------------------------------------------
# 排版与间距
# --------------------------------------------------------------------------
# 字号阶梯：原先所有文字都是 10pt，导致标题、表格、状态栏毫无层级。
# 这里给出明确的分级（单位 pt，tkinter 负责按 DPI 缩放）。
FONT_SIZE_TITLE = 15
FONT_SIZE_BODY = 10
FONT_SIZE_TABLE = 10
FONT_SIZE_CAPTION = 9

# 间距标尺：4pt 基准。刻意保留跨度较大的档位，
# 让「同组元素紧凑、不同组之间留白」的节奏能真正做出来。
SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 12
SPACE_LG = 16
SPACE_XL = 24

# 表格行高。原先 27px 偏挤；数字类表格适当加高更易横向扫读。
TABLE_ROW_HEIGHT = 30


def critical_contrast_pairs(palette: Palette) -> tuple[tuple[str, str, str], ...]:
    """列出界面上真实出现、且需要满足 AA 的前景/背景组合。

    刻意把斑马纹行、系统进程行、开发端口强调这些「实际渲染出来的」组合
    也列进来——只校验正文配纯背景很容易漏掉表格里那些真正难读的地方。
    """
    return (
        ("正文/表面", palette.text_primary, palette.surface),
        ("正文/斑马纹", palette.text_primary, palette.row_stripe),
        ("正文/窗口", palette.text_primary, palette.window),
        # 开发端口行与系统进程行都是「带底色的整行」，正文落在这些底色上，
        # 是表格里最容易被忽略、也最容易不达标的位置。
        ("正文/开发端口底", palette.text_primary, palette.accent_soft),
        ("次要文字/窗口", palette.text_secondary, palette.window),
        ("次要文字/表面", palette.text_secondary, palette.surface),
        ("主色文字/主色块", palette.accent_text, palette.accent),
        ("危险文字/危险块", palette.danger_text, palette.danger),
        ("开发端口色/表面", palette.accent, palette.surface),
        ("警示色/表面", palette.caution, palette.surface),
        ("警示色/系统进程底", palette.caution, palette.caution_soft),
        ("选中文字/选中底", palette.selection_text, palette.selection),
    )


def describe_palette(palette: Palette) -> str:
    """输出关键组合的对比度，便于人工与自动校验。"""
    lines = [f"[{palette.name}]"]
    for label, foreground, background in critical_contrast_pairs(palette):
        ratio = contrast_ratio(foreground, background)
        verdict = "AA" if ratio >= 4.5 else "!!"
        lines.append(f"  {verdict}  {label:<22} {ratio:5.2f}:1")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    for current_palette in (LIGHT_PALETTE, DARK_PALETTE):
        print(describe_palette(current_palette))
        print()
