#!/usr/bin/env python3
"""把 Release 说明模板里的占位符替换为本次构建的实际值。

放成独立脚本而不是内联进 workflow，是为了避开 PowerShell here-string 的
缩进/反引号转义陷阱，同时让模板内容保持可读、可单独编辑。

用法:
  python .github/scripts/render_release_notes.py \
      --sha256 <hash> --repository owner/name --output release_notes.md
"""

from __future__ import annotations

import argparse
from pathlib import Path

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "release_notes_template.md"


def render_release_notes(sha256: str, repository: str) -> str:
    template_text = TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {"{{SHA256}}": sha256, "{{REPOSITORY}}": repository}

    rendered_text = template_text
    for placeholder, value in replacements.items():
        if placeholder not in rendered_text:
            raise SystemExit(f"模板里找不到占位符 {placeholder}，请检查 {TEMPLATE_PATH.name}")
        rendered_text = rendered_text.replace(placeholder, value)
    return rendered_text


def main() -> int:
    parser = argparse.ArgumentParser(description="渲染 Release 说明")
    parser.add_argument("--sha256", required=True, help="产物的 SHA256 校验值")
    parser.add_argument("--repository", required=True, help="仓库全名，如 owner/name")
    parser.add_argument("--output", required=True, help="输出文件路径")
    arguments = parser.parse_args()

    rendered_text = render_release_notes(arguments.sha256, arguments.repository)
    # 显式写 UTF-8 无 BOM，避免 GitHub 上中文出现乱码或多余字符。
    Path(arguments.output).write_text(rendered_text, encoding="utf-8", newline="\n")
    print(f"已生成 {arguments.output}（{len(rendered_text)} 字符）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
