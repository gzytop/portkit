#!/usr/bin/env python3
"""把 Release 说明模板里的占位符替换为本次构建的实际值。

「本次更新」一节从 CHANGELOG.md 里按版本号抽取。这样做的原因是此前模板完全静态，
v1.0.0 到 v1.2.1 的说明除 SHA256 外逐字节相同，用户无法从 Release 页面看出改了什么。

放成独立脚本而不是内联进 workflow，是为了避开 PowerShell here-string 的
缩进/反引号转义陷阱，同时让模板内容保持可读、可单独编辑。

用法:
  python .github/scripts/render_release_notes.py \
      --sha256 <hash> --repository owner/name --tag v1.2.2 --output release_notes.md
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATE_PATH = REPOSITORY_ROOT / ".github" / "release_notes_template.md"
CHANGELOG_PATH = REPOSITORY_ROOT / "CHANGELOG.md"

# CHANGELOG.md 里的版本小节标题，例如 "## v1.2.1 — 2026-08-18"。
# 破折号与日期可选，方便本地先写标题、发版前再补日期。
VERSION_HEADING_PATTERN = re.compile(r"^##\s+(v\d+\.\d+\.\d+)\s*(?:—.*)?$", re.MULTILINE)


def allow_non_ascii_output() -> None:
    """CI 的 Windows runner 控制台是 cp1252，打印中文会抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            try:
                stream.reconfigure(errors="replace")
            except (AttributeError, ValueError):
                pass


def normalize_version_tag(tag: str) -> str:
    """把 refs/tags/v1.2.1 之类的输入统一成 v1.2.1。"""
    return tag.strip().rsplit("/", maxsplit=1)[-1]


def extract_changelog_section(changelog_text: str, version_tag: str) -> str:
    """取出指定版本在 CHANGELOG 里的正文（不含标题行）。

    找不到该版本时直接抛错让构建失败。宁可发版中断，也不要静默退回通用文案——
    那正是此前每个 Release 说明都长得一样的原因。
    """
    headings = list(VERSION_HEADING_PATTERN.finditer(changelog_text))
    for index, heading in enumerate(headings):
        if heading.group(1) != version_tag:
            continue
        body_start = heading.end()
        body_end = headings[index + 1].start() if index + 1 < len(headings) else len(changelog_text)
        section_text = changelog_text[body_start:body_end].strip()
        if not section_text:
            raise SystemExit(
                f"{CHANGELOG_PATH.name} 里 {version_tag} 的小节是空的，请补上本次更新内容"
            )
        return section_text

    known_versions = ", ".join(heading.group(1) for heading in headings) or "（一个都没有）"
    raise SystemExit(
        f"{CHANGELOG_PATH.name} 里找不到 {version_tag} 的小节。\n"
        f"发版前请把「## 未发布」改名为「## {version_tag} — YYYY-MM-DD」并填写本次更新内容。\n"
        f"当前已有的版本小节：{known_versions}"
    )


def render_release_notes(sha256: str, repository: str, version_tag: str) -> str:
    template_text = TEMPLATE_PATH.read_text(encoding="utf-8")
    changelog_text = CHANGELOG_PATH.read_text(encoding="utf-8")

    replacements = {
        "{{SHA256}}": sha256,
        "{{REPOSITORY}}": repository,
        "{{VERSION}}": version_tag,
        "{{CHANGES}}": extract_changelog_section(changelog_text, version_tag),
    }

    rendered_text = template_text
    for placeholder, value in replacements.items():
        if placeholder not in rendered_text:
            raise SystemExit(f"模板里找不到占位符 {placeholder}，请检查 {TEMPLATE_PATH.name}")
        rendered_text = rendered_text.replace(placeholder, value)
    return rendered_text


def main() -> int:
    allow_non_ascii_output()
    parser = argparse.ArgumentParser(description="渲染 Release 说明")
    parser.add_argument("--sha256", required=True, help="产物的 SHA256 校验值")
    parser.add_argument("--repository", required=True, help="仓库全名，如 owner/name")
    parser.add_argument("--tag", required=True, help="本次发布的 tag，如 v1.2.2")
    parser.add_argument("--output", required=True, help="输出文件路径")
    arguments = parser.parse_args()

    version_tag = normalize_version_tag(arguments.tag)
    rendered_text = render_release_notes(arguments.sha256, arguments.repository, version_tag)
    # 显式写 UTF-8 无 BOM，避免 GitHub 上中文出现乱码或多余字符。
    Path(arguments.output).write_text(rendered_text, encoding="utf-8", newline="\n")
    print(f"已生成 {arguments.output}（{version_tag}，{len(rendered_text)} 字符）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
