"""精确编辑工具。"""

from __future__ import annotations

import difflib
import aiofiles
from typing import Annotated

from src.app.plugin_system.base import BaseTool

from .base import CodingToolMixin


class EditTool(CodingToolMixin, BaseTool):
    """精确编辑文件局部内容（搜索替换）。"""

    tool_name = "edit"
    tool_description = (
        "Make targeted edits to a file. Specify exact text to find and "
        "replace. More efficient than rewriting entire files."
    )
    chatter_allow = ["coding_agent"]

    async def execute(
        self,
        path: Annotated[str, "File path to edit"],
        old_text: Annotated[str, "Exact text to find (must match precisely including whitespace)"],
        new_text: Annotated[str, "Replacement text"],
    ) -> tuple[bool, str]:
        """精确编辑文件局部内容。"""
        self._ensure_not_interrupted()
        try:
            target = self._resolve_path(path)
            self._check_path_safety(path)
        except ValueError as e:
            return False, str(e)

        self._observe_staging_path(target)

        # 检查暂存区：优先从暂存区读取当前内容
        staging = self._get_staging_area()

        if staging:
            # 从暂存区或磁盘读取当前内容
            current = staging.get_staged_content(str(target))
            if current is None:
                if not target.exists():
                    return False, f"文件不存在: {target}"
                try:
                    async with aiofiles.open(target, "r", encoding="utf-8", errors="replace") as f:
                        current = await f.read()
                except OSError as e:
                    return False, f"读取文件失败: {e}"

            # 查找出现次数
            count = current.count(old_text)

            if count == 0:
                lines = current.splitlines()
                old_lines = old_text.splitlines()
                suggestions: list[str] = []
                for i, line in enumerate(lines):
                    for old_line in old_lines:
                        if old_line.strip() and old_line.strip() in line:
                            suggestions.append(f"  行 {i+1}: {line.strip()[:80]}")
                            if len(suggestions) >= 5:
                                break
                    if len(suggestions) >= 5:
                        break

                msg = "未找到精确匹配的内容"
                if suggestions:
                    msg += "，可能的近似位置:\n" + "\n".join(suggestions)
                return False, msg

            if count > 1:
                return False, (
                    f"找到 {count} 处匹配，请提供更多上下文使匹配唯一。"
                    f"当前 old_text 在文件中出现 {count} 次。"
                )

            # 创建 checkpoint 快照
            checkpoint_mgr = self._get_checkpoint_manager()
            checkpoint = None
            if checkpoint_mgr:
                agent_name = getattr(self, "agent_name", "coding_agent")
                checkpoint = await checkpoint_mgr.snapshot_before_write(
                    str(target), agent_name, f"edit: {path}", tool_name="edit"
                )

            # 替换
            self._ensure_not_interrupted()
            new_content = current.replace(old_text, new_text, 1)

            # 暂存变更
            try:
                staged_action = staging.stage_edit(str(target), current, new_content)
            except OSError as e:
                return False, f"写入文件失败: {e}"

            # 生成 unified diff
            original_lines = current.splitlines(keepends=True)
            new_lines = new_content.splitlines(keepends=True)
            diff_lines = list(difflib.unified_diff(
                original_lines, new_lines,
                fromfile=f"a/{path}", tofile=f"b/{path}",
                lineterm="",
            ))
            diff_text = "".join(diff_lines) if diff_lines else "(无差异)"

            # 通知
            await self._notify_file_change(path, staged_action, diff_text)
            if checkpoint:
                await self._notify_checkpoint_created(checkpoint)

            return True, f"[暂存] 已编辑 {path}:\n```diff\n{diff_text}\n```"

        # 无暂存区：走原有磁盘编辑逻辑

        if not target.exists():
            return False, f"文件不存在: {target}"

        # 读取原文件内容
        try:
            async with aiofiles.open(target, "r", encoding="utf-8", errors="replace") as f:
                original = await f.read()
        except OSError as e:
            return False, f"读取文件失败: {e}"

        # 查找出现次数
        count = original.count(old_text)

        if count == 0:
            # 尝试提供近似匹配提示
            lines = original.splitlines()
            old_lines = old_text.splitlines()
            suggestions: list[str] = []
            for i, line in enumerate(lines):
                for old_line in old_lines:
                    if old_line.strip() and old_line.strip() in line:
                        suggestions.append(f"  行 {i+1}: {line.strip()[:80]}")
                        if len(suggestions) >= 5:
                            break
                if len(suggestions) >= 5:
                    break

            msg = "未找到精确匹配的内容"
            if suggestions:
                msg += "，可能的近似位置:\n" + "\n".join(suggestions)
            return False, msg

        if count > 1:
            return False, (
                f"找到 {count} 处匹配，请提供更多上下文使匹配唯一。"
                f"当前 old_text 在文件中出现 {count} 次。"
            )

        # 创建 checkpoint 快照
        checkpoint_mgr = self._get_checkpoint_manager()
        checkpoint = None
        if checkpoint_mgr:
            agent_name = getattr(self, "agent_name", "coding_agent")
            checkpoint = await checkpoint_mgr.snapshot_before_write(
                str(target), agent_name, f"edit: {path}", tool_name="edit"
            )

        # 替换
        self._ensure_not_interrupted()
        new_content = original.replace(old_text, new_text, 1)

        # 写回文件
        self._ensure_not_interrupted()
        try:
            async with aiofiles.open(target, "w", encoding="utf-8", newline="") as f:
                await f.write(new_content)
        except OSError as e:
            return False, f"写入文件失败: {e}"

        # 生成 unified diff
        original_lines = original.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff_lines = list(difflib.unified_diff(
            original_lines, new_lines,
            fromfile=f"a/{path}", tofile=f"b/{path}",
            lineterm="",
        ))
        diff_text = "".join(diff_lines) if diff_lines else "(无差异)"

        # 通知
        await self._notify_file_change(path, "modify", diff_text)
        if checkpoint:
            await self._notify_checkpoint_created(checkpoint)

        return True, f"已编辑 {path}:\n```diff\n{diff_text}\n```"
