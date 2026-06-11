"""文件读取工具。"""

from __future__ import annotations

import aiofiles
from pathlib import Path
from typing import Annotated

from src.app.plugin_system.base import BaseTool

from .base import CodingToolMixin


class ReadTool(CodingToolMixin, BaseTool):
    """读取文件内容。"""

    tool_name = "read"
    tool_description = "Read file contents, optionally within a line range"
    chatter_allow = ["coding_agent"]

    async def execute(
        self,
        path: Annotated[str, "File path (relative to project root or absolute)"],
        start_line: Annotated[int, "Start line, 1-indexed. 0 means from beginning"] = 0,
        end_line: Annotated[int, "End line, 1-indexed. 0 means to end"] = 0,
    ) -> tuple[bool, str]:
        """读取文件内容，支持行号范围。"""
        try:
            target = self._resolve_path(path)
            self._check_path_safety(path)
        except ValueError as e:
            return False, str(e)

        if not target.exists():
            return False, f"文件不存在: {target}"

        if not target.is_file():
            return False, f"路径不是文件: {target}"

        # 检测二进制文件
        try:
            async with aiofiles.open(target, "rb") as f:
                head = await f.read(512)
                if b"\x00" in head:
                    return False, f"文件是二进制格式，无法读取: {target}"
        except OSError as e:
            return False, f"读取文件失败: {e}"

        # 读取全部内容
        try:
            async with aiofiles.open(target, "r", encoding="utf-8", errors="replace") as f:
                content = await f.read()
        except OSError as e:
            return False, f"读取文件失败: {e}"

        lines = content.splitlines(keepends=True)
        total_lines = len(lines)

        # 如果文件过大且未指定行号范围，截断显示
        if total_lines > 500 and start_line == 0 and end_line == 0:
            head_lines = lines[:50]
            tail_lines = lines[-50:]
            head_text = self._format_lines(head_lines, start=1)
            tail_text = self._format_lines(tail_lines, start=total_lines - 49)
            omitted = total_lines - 100
            return True, (
                f"文件共 {total_lines} 行，显示前 50 行和后 50 行"
                f"（省略中间 {omitted} 行，请使用 start_line/end_line 查看）：\n\n"
                f"{head_text}\n"
                f"  ... (省略 {omitted} 行) ...\n\n"
                f"{tail_text}"
            )

        # 应用行号范围
        if start_line > 0 or end_line > 0:
            s = max(start_line - 1, 0) if start_line > 0 else 0
            e = end_line if end_line > 0 else total_lines
            lines = lines[s:e]
            display_start = s + 1
        else:
            display_start = 1

        formatted = self._format_lines(lines, start=display_start)
        return True, f"文件 {path} ({total_lines} 行):\n{formatted}"

    @staticmethod
    def _format_lines(lines: list[str], start: int) -> str:
        """格式化行号为 '  42 | code here' 格式。"""
        result = []
        for i, line in enumerate(lines):
            line_num = start + i
            # 去掉末尾的换行符用于显示
            line_content = line.rstrip("\n").rstrip("\r")
            result.append(f"{line_num:>6} | {line_content}")
        return "\n".join(result)
