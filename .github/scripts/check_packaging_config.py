#!/usr/bin/env python3
"""校验 PyInstaller 打包配置引用的文件确实存在。

CI 不在每个平台真打包（耗时且只有 Windows 有意义），但至少要保证 spec
引用的文件没有被改名或删掉——否则问题会一直潜伏到发版当天才暴露。

用法:
  python .github/scripts/check_packaging_config.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SPEC_PATH = PROJECT_ROOT / "portkit.spec"

# spec 里通过文件名引用的资源；只检查确实被引用到的，避免误报。
REFERENCED_FILE_NAMES = ("portkit_gui.py", "app_icon.ico", "version_info.txt")

# PyInstaller 靠跟踪 import 链收集这些模块，spec 里不会出现它们的文件名。
# 但少了任何一个，打出来的 exe 都会在启动时因 ModuleNotFoundError 直接退出，
# 所以这里单独把入口模块的依赖列出来守住。
REQUIRED_IMPORT_MODULES = ("portkit.py", "theme.py")


def configure_output_encoding() -> None:
    """CI 的 Windows runner 控制台是 cp1252，中文输出会抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            try:
                stream.reconfigure(errors="replace")
            except (AttributeError, ValueError):
                pass


def find_missing_referenced_files() -> list[str]:
    spec_text = SPEC_PATH.read_text(encoding="utf-8")
    return [
        file_name
        for file_name in REFERENCED_FILE_NAMES
        if file_name in spec_text and not (PROJECT_ROOT / file_name).exists()
    ]


def find_missing_import_modules() -> list[str]:
    return [
        module_name
        for module_name in REQUIRED_IMPORT_MODULES
        if not (PROJECT_ROOT / module_name).exists()
    ]


def main() -> int:
    configure_output_encoding()

    if not SPEC_PATH.exists():
        print(f"找不到打包配置 {SPEC_PATH.name}", file=sys.stderr)
        return 1

    missing_files = find_missing_referenced_files()
    if missing_files:
        print(f"打包配置引用了不存在的文件: {', '.join(missing_files)}", file=sys.stderr)
        return 1

    missing_modules = find_missing_import_modules()
    if missing_modules:
        print(f"入口模块依赖的文件不存在: {', '.join(missing_modules)}", file=sys.stderr)
        return 1

    print("打包配置引用的文件与入口依赖模块都存在")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
