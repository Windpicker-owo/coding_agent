"""目录列表工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from src.app.plugin_system.base import BaseTool

from .base import CodingToolMixin

# 默认排除的目录
_EXCLUDE_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "htmlcov",
    ".uv-cache", ".uv-cache-local", "dist", "build", ".eggs",
    ".coverage", ".DS_Store",
}


class LsTool(CodingToolMixin, BaseTool):
    """列出目录内容。"""

    tool_name = "ls"
    tool_description = "List directory contents with file sizes and types"
    chatter_allow = ["coding_agent"]

    async def execute(
        self,
        path: Annotated[str, "Directory path (default: project root)"] = ".",
        depth: Annotated[int, "Listing depth, 1 = immediate children"] = 1,
        show_hidden: Annotated[bool, "Show hidden files/dirs"] = False,
    ) -> tuple[bool, str]:
        """列出目录内容，支持指定深度。"""
        try:
            target = self._resolve_path(path)
            self._check_path_safety(path)
        except ValueError as e:
            return False, str(e)

        if not target.exists():
            return False, f"目录不存在: {target}"

        if not target.is_dir():
            return False, f"路径不是目录: {target}"

        lines: list[str] = []
        self._build_tree(target, lines, depth=depth, current_depth=1,
                         show_hidden=show_hidden, prefix="")

        if not lines:
            return True, f"目录 {path} 为空"

        header = f"目录 {path}:\n"
        return True, header + "\n".join(lines)

    def _build_tree(
        self,
        directory: Path,
        lines: list[str],
        depth: int,
        current_depth: int,
        show_hidden: bool,
        prefix: str,
    ) -> None:
        """递归构建目录树。"""
        try:
            entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            lines.append(f"{prefix}[权限不足]")
            return

        # 过滤隐藏文件和排除目录
        filtered: list[Path] = []
        for entry in entries:
            name = entry.name
            if not show_hidden and name.startswith("."):
                continue
            if entry.is_dir() and name in _EXCLUDE_DIRS:
                continue
            filtered.append(entry)

        for i, entry in enumerate(filtered):
            is_last = i == len(filtered) - 1
            connector = "└── " if is_last else "├── "
            child_prefix = "    " if is_last else "│   "

            if entry.is_dir():
                try:
                    item_count = len(list(entry.iterdir()))
                except PermissionError:
                    item_count = -1
                count_str = f"{item_count} items" if item_count >= 0 else "?"
                lines.append(f"{prefix}{connector}{entry.name}/  [dir, {count_str}]")

                # 递归子目录
                if current_depth < depth:
                    self._build_tree(
                        entry, lines, depth=depth,
                        current_depth=current_depth + 1,
                        show_hidden=show_hidden,
                        prefix=prefix + child_prefix,
                    )
            else:
                size = self._format_size(entry)
                lines.append(f"{prefix}{connector}{entry.name}  [file, {size}]")

    @staticmethod
    def _format_size(path: Path) -> str:
        """格式化文件大小。"""
        try:
            size = path.stat().st_size
        except OSError:
            return "?"

        if size < 1024:
            return f"{size}B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f}KB"
        else:
            return f"{size / (1024 * 1024):.1f}MB"
