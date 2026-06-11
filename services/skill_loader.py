"""Skill 目录加载器。

扫描项目根目录下的 `.agents/skills/` 目录，发现所有标准 skill（含 SKILL.md 的子目录），
解析 front matter 获取名称与描述，构建 catalog 供 system_reminder 注入。

标准 skill 形式（与 skill_manager 插件一致）：
- 每个 skill 是一个目录，目录名即默认 skill 名称
- 目录内必须包含 `SKILL.md`
- SKILL.md 开头可选 YAML front matter：
  ---
  name: 显示名称
  description: 简要描述
  ---
  （正文为 skill 详细指令）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_FRONT_MATTER_FIELD_RE = re.compile(r"^(name|description)\s*:\s*(.+)$", re.IGNORECASE)


def _strip_quoted_text(value: str) -> str:
    """去除首尾引号并清理空白。"""
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1].strip()
    return text


def _parse_skill_front_matter(raw_text: str) -> tuple[str | None, str | None]:
    """从 SKILL.md 首段 front matter 提取 name 和 description。"""
    lines = raw_text.splitlines()
    if len(lines) < 3:
        return None, None
    if lines[0].strip() != "---":
        return None, None

    parsed_name: str | None = None
    parsed_description: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        matched = _FRONT_MATTER_FIELD_RE.match(line.strip())
        if not matched:
            continue
        key, value = matched.groups()
        normalized_value = _strip_quoted_text(value)
        if key.lower() == "name":
            parsed_name = normalized_value
        elif key.lower() == "description":
            parsed_description = normalized_value
    return parsed_name, parsed_description


@dataclass(slots=True)
class SkillEntry:
    """发现的 skill 条目信息。"""
    name: str
    description: str
    root_dir: Path
    skill_md_path: Path


def discover_skills(working_directory: str) -> list[SkillEntry]:
    """扫描项目 skill 目录，返回所有发现的 skill。

    扫描 `<working_directory>/.agents/skills/`，找出所有直接包含 SKILL.md 的子目录。
    每个 skill 的名称取自 front matter 的 name 字段，回退为目录名。

    Args:
        working_directory: 项目根目录路径

    Returns:
        SkillEntry 列表（按名称排序）
    """
    skills_dir = Path(working_directory) / ".agents" / "skills"
    if not skills_dir.is_dir():
        return []

    entries: list[SkillEntry] = []
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir():
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.is_file():
            continue

        raw_text = skill_md.read_text(encoding="utf-8")
        parsed_name, parsed_description = _parse_skill_front_matter(raw_text)
        name = (parsed_name or child.name).strip()
        description = (parsed_description or f"Skill {name}，通过 read 读取 SKILL.md 获取详细指令").strip()

        entries.append(SkillEntry(
            name=name,
            description=description,
            root_dir=child,
            skill_md_path=skill_md,
        ))

    entries.sort(key=lambda e: e.name.lower())
    return entries


def build_skills_catalog(working_directory: str) -> str | None:
    """构建 skills 目录提醒文本。

    扫描项目 skills 并渲染为简短的目录列表，告知 Agent 可用的 skills
    及如何通过 read 工具获取详细信息。

    Args:
        working_directory: 项目根目录路径

    Returns:
        Markdown 格式的 skills catalog 文本，无 skill 时返回 None
    """
    entries = discover_skills(working_directory)
    if not entries:
        return None

    lines = [
        "## 可用 Skills",
        "以下 skills 可通过 `read` 工具读取对应的 SKILL.md 获取详细指令。",
        "需要某项专项能力时，先读取其 SKILL.md，再按指令操作。",
        "",
    ]
    for entry in entries:
        rel_path = entry.skill_md_path.relative_to(Path(working_directory))
        lines.append(f"- **{entry.name}**: {entry.description} （`{rel_path}`）")

    return "\n".join(lines)
