"""文件读取工具。"""

from __future__ import annotations

import base64
import aiofiles
from typing import Annotated

from src.app.plugin_system.base import BaseTool
from src.kernel.logger import get_logger

from .base import CodingToolMixin

logger = get_logger("coding_agent.read")

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico", ".tiff", ".tif"}

MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
}


class ReadTool(CodingToolMixin, BaseTool):
    """读取文件内容。"""

    tool_name = "read"
    tool_description = (
        "Read file contents, optionally within a line range. "
        "Line endings are preserved as-is; the output header reports "
        "the detected newline style (CRLF/LF/mixed). "
        "Use start_line/end_line (1-indexed, 0=from start/to end) to view ranges."
    )
    chatter_allow = ["coding_agent"]

    async def execute(
        self,
        path: Annotated[str, "File path (relative to project root or absolute)"],
        start_line: Annotated[int, "Start line, 1-indexed. 0 means from beginning"] = 0,
        end_line: Annotated[int, "End line, 1-indexed. 0 means to end"] = 0,
    ) -> tuple[bool, str]:
        """读取文件内容，支持行号范围。"""
        self._ensure_not_interrupted()
        try:
            target = self._resolve_path(path)
            self._check_path_safety(path)
        except ValueError as e:
            return False, str(e)

        self._observe_staging_path(target)

        # 检查暂存区：优先返回暂存内容
        staging = self._get_staging_area()
        if staging and staging.has_staged(str(target)):
            staged_content = staging.get_staged_content(str(target))
            if staged_content is not None:
                content = staged_content
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
                        f"[暂存] 文件共 {total_lines} 行，显示前 50 行和后 50 行"
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
                return True, f"[暂存] 文件 {path} ({total_lines} 行):\n{formatted}"

        # 无暂存区或文件未暂存：走原有磁盘读取逻辑

        if not target.exists():
            return False, f"文件不存在: {target}"

        if not target.is_file():
            return False, f"路径不是文件: {target}"

        # 检测图像文件：读取为 base64 返回
        suffix = target.suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            try:
                async with aiofiles.open(target, "rb") as f:
                    raw_bytes = await f.read()
                encoded = base64.b64encode(raw_bytes).decode("ascii")
                mime = MIME_MAP.get(suffix, "image/png")
                size_kb = len(raw_bytes) / 1024
                return True, (
                    f"图像文件 {path} ({size_kb:.1f} KB, {mime}):\n"
                    f"data:{mime};base64,{encoded}"
                )
            except OSError as e:
                return False, f"读取图像文件失败: {e}"

        # 检测二进制文件
        try:
            async with aiofiles.open(target, "rb") as f:
                head = await f.read(512)
                if b"\x00" in head:
                    return False, f"文件是二进制格式，无法读取: {target}"
        except OSError as e:
            return False, f"读取文件失败: {e}"

        # 读取全部内容（newline="" 保持原始换行符）
        try:
            async with aiofiles.open(target, "r", encoding="utf-8", errors="replace", newline="") as f:
                content = await f.read()
        except OSError as e:
            return False, f"读取文件失败: {e}"

        self._warn_on_replace_char(content, str(target))

        lines = content.splitlines(keepends=True)
        total_lines = len(lines)
        newline_type = self._detect_newline_type(lines)

        # 如果文件过大且未指定行号范围，截断显示
        if total_lines > 500 and start_line == 0 and end_line == 0:
            head_lines = lines[:50]
            tail_lines = lines[-50:]
            head_text = self._format_lines(head_lines, start=1)
            tail_text = self._format_lines(tail_lines, start=total_lines - 49)
            omitted = total_lines - 100
            return True, (
                f"文件共 {total_lines} 行，{newline_type}，显示前 50 行和后 50 行"
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
        return True, f"文件 {path} ({total_lines} 行, {newline_type}):\n{formatted}"

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

    @staticmethod
    def _detect_newline_type(lines: list[str]) -> str:
        """检测文件的换行符类型。"""
        crlf = sum(1 for line in lines if line.endswith("\r\n"))
        lf = sum(1 for line in lines if line.endswith("\n") and not line.endswith("\r\n"))
        cr = sum(1 for line in lines if line.endswith("\r") and not line.endswith("\r\n"))
        if crlf > 0 and lf == 0 and cr == 0:
            return "CRLF"
        elif lf > 0 and crlf == 0 and cr == 0:
            return "LF"
        elif cr > 0 and crlf == 0 and lf == 0:
            return "CR"
        elif crlf == 0 and lf == 0 and cr == 0:
            return "no EOL"
        else:
            return "mixed"

    @staticmethod
    def _warn_on_replace_char(content: str, path: str) -> None:
        """若内容包含替换字符 \ufffd，发出警告。"""
        if "\ufffd" in content:
            positions = [i for i, ch in enumerate(content) if ch == "\ufffd"]
            line_nums = {content[:pos].count("\n") + 1 for pos in positions[:10]}
            logger.warning(
                f"文件 {path} 包含非 UTF-8 字节序列，已替换为 \\ufffd，"
                f"涉及行号: {sorted(line_nums)[:10]}"
            )
