"""文本搜索工具。"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Annotated

from src.app.plugin_system.base import BaseTool
from src.kernel.logger import get_logger

from .base import CodingToolMixin

logger = get_logger("coding_agent.grep")


class GrepTool(CodingToolMixin, BaseTool):
    """在文件或目录中搜索文本模式。"""

    tool_name = "grep"
    tool_description = "Search for text patterns in files using regex"
    chatter_allow = ["coding_agent"]

    async def execute(
        self,
        pattern: Annotated[str, "Search pattern (regex supported)"],
        path: Annotated[str, "Search path (file or directory)"] = ".",
        include: Annotated[str, "File glob filter, e.g. '*.py'"] = "",
        case_insensitive: Annotated[bool, "Case-insensitive search"] = False,
        max_results: Annotated[int, "Maximum number of results"] = 50,
    ) -> tuple[bool, str]:
        """搜索文本模式，优先使用 ripgrep。"""
        try:
            target = self._resolve_path(path)
            self._check_path_safety(path)
        except ValueError as e:
            return False, str(e)

        if not target.exists():
            return False, f"路径不存在: {target}"

        # 检测是否有 ripgrep
        use_rg = shutil.which("rg") is not None

        if use_rg:
            return await self._search_with_rg(target, pattern, include, case_insensitive, max_results)
        else:
            return await self._search_with_grep(target, pattern, include, case_insensitive, max_results)

    async def _search_with_rg(
        self, target: Path, pattern: str, include: str,
        case_insensitive: bool, max_results: int,
    ) -> tuple[bool, str]:
        """使用 ripgrep 搜索。"""
        cmd = ["rg", "--no-heading", "-n"]

        if case_insensitive:
            cmd.append("-i")
        if include:
            cmd.extend(["--glob", include])
        cmd.extend(["-m", str(max_results)])
        cmd.extend([pattern, str(target)])

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=30
            )
        except asyncio.TimeoutError:
            return False, "搜索超时（30秒）"
        except OSError as e:
            return False, f"执行 ripgrep 失败: {e}"

        raw_output = stdout.decode("utf-8", errors="replace")
        self._warn_on_replace_char(raw_output, "rg stdout")
        output = raw_output.strip()

        if process.returncode == 1 and not output:
            return True, f"未找到匹配 '{pattern}' 的结果"

        if process.returncode not in (0, 1):
            raw_err = stderr.decode("utf-8", errors="replace")
            self._warn_on_replace_char(raw_err, "rg stderr")
            err = raw_err.strip()
            return False, f"ripgrep 错误: {err}"

        lines = output.splitlines()
        if len(lines) > max_results:
            lines = lines[:max_results]
            output = "\n".join(lines) + f"\n\n... 结果已截断（超过 {max_results} 条）"

        return True, output if output else f"未找到匹配 '{pattern}' 的结果"

    async def _search_with_grep(
        self, target: Path, pattern: str, include: str,
        case_insensitive: bool, max_results: int,
    ) -> tuple[bool, str]:
        """降级使用 grep 搜索。"""
        cmd = ["grep", "-rn"]

        if case_insensitive:
            cmd.append("-i")
        if include:
            cmd.append(f"--include={include}")

        cmd.extend([pattern, str(target)])

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=30
            )
        except asyncio.TimeoutError:
            return False, "搜索超时（30秒）"
        except OSError as e:
            return False, f"执行 grep 失败: {e}"

        raw_output = stdout.decode("utf-8", errors="replace")
        self._warn_on_replace_char(raw_output, "grep stdout")
        output = raw_output.strip()

        if process.returncode == 1 and not output:
            return True, f"未找到匹配 '{pattern}' 的结果"

        if process.returncode not in (0, 1):
            raw_err = stderr.decode("utf-8", errors="replace")
            self._warn_on_replace_char(raw_err, "grep stderr")
            err = raw_err.strip()
            return False, f"grep 错误: {err}"

        lines = output.splitlines()
        if len(lines) > max_results:
            lines = lines[:max_results]
            output = "\n".join(lines) + f"\n\n... 结果已截断（超过 {max_results} 条）"

        return True, output if output else f"未找到匹配 '{pattern}' 的结果"

    @staticmethod
    def _warn_on_replace_char(content: str, source: str) -> None:
        """若内容包含替换字符 \\ufffd，发出警告。"""
        if "\ufffd" in content:
            positions = [i for i, ch in enumerate(content) if ch == "\ufffd"]
            logger.warning(
                f"{source} 包含非 UTF-8 字节序列，已替换为 \\ufffd，"
                f"出现 {len(positions)} 处"
            )
