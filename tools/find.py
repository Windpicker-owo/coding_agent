"""文件查找工具。"""

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
}


class FindTool(CodingToolMixin, BaseTool):
    """按文件名模式搜索文件。"""

    tool_name = "find"
    tool_description = "Find files by name pattern in the project"
    chatter_allow = ["coding_agent"]

    async def execute(
        self,
        pattern: Annotated[str, "Filename glob pattern, e.g. '*.py', 'test_*'"],
        path: Annotated[str, "Search root directory"] = ".",
        max_depth: Annotated[int, "Max search depth, 0 = unlimited"] = 0,
        file_type: Annotated[str, "'file', 'dir', or 'all'"] = "all",
    ) -> tuple[bool, str]:
        """按 glob 模式查找文件。"""
        try:
            target = self._resolve_path(path)
            self._check_path_safety(path)
        except ValueError as e:
            return False, str(e)

        if not target.exists():
            return False, f"目录不存在: {target}"

        if not target.is_dir():
            return False, f"路径不是目录: {target}"

        results: list[str] = []
        max_results = 100

        self._walk_directory(
            directory=target,
            pattern=pattern,
            file_type=file_type,
            max_depth=max_depth,
            current_depth=0,
            base=target,
            results=results,
            max_results=max_results,
        )

        if not results:
            return True, f"未找到匹配 '{pattern}' 的文件"

        output = "\n".join(results)
        if len(results) >= max_results:
            output += f"\n\n... 结果已截断（超过 {max_results} 条）"

        return True, f"找到 {len(results)} 个匹配项:\n{output}"

    def _walk_directory(
        self,
        directory: Path,
        pattern: str,
        file_type: str,
        max_depth: int,
        current_depth: int,
        base: Path,
        results: list[str],
        max_results: int,
    ) -> None:
        """递归遍历目录查找匹配文件。"""
        if len(results) >= max_results:
            return

        if max_depth > 0 and current_depth >= max_depth:
            return

        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        except PermissionError:
            return

        for entry in entries:
            if len(results) >= max_results:
                return

            name = entry.name

            # 跳过排除目录
            if entry.is_dir() and name in _EXCLUDE_DIRS:
                continue

            # 跳过隐藏文件/目录
            if name.startswith("."):
                continue

            # 检查是否匹配模式
            matched = False
            if file_type == "all":
                matched = self._match_pattern(name, pattern)
            elif file_type == "file" and entry.is_file():
                matched = self._match_pattern(name, pattern)
            elif file_type == "dir" and entry.is_dir():
                matched = self._match_pattern(name, pattern)

            if matched:
                try:
                    rel_path = entry.relative_to(base)
                except ValueError:
                    rel_path = entry
                results.append(str(rel_path))

            # 递归子目录
            if entry.is_dir():
                self._walk_directory(
                    directory=entry,
                    pattern=pattern,
                    file_type=file_type,
                    max_depth=max_depth,
                    current_depth=current_depth + 1,
                    base=base,
                    results=results,
                    max_results=max_results,
                )

    @staticmethod
    def _match_pattern(name: str, pattern: str) -> bool:
        """简单的 glob 模式匹配。"""
        import fnmatch
        return fnmatch.fnmatch(name, pattern)
