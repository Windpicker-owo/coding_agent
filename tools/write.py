"""文件创建/覆盖工具。"""

from __future__ import annotations

import difflib
import aiofiles
from typing import Annotated

from src.app.plugin_system.base import BaseTool

from .base import CodingToolMixin


class WriteTool(CodingToolMixin, BaseTool):
    """创建文件或覆盖整个文件内容。"""

    tool_name = "write"
    tool_description = "Create a new file or overwrite an existing file entirely"
    chatter_allow = ["coding_agent"]

    async def execute(
        self,
        path: Annotated[str, "File path to write"],
        content: Annotated[str, "Complete file content"],
        create_dirs: Annotated[bool, "Auto-create parent directories"] = True,
    ) -> tuple[bool, str]:
        """写入文件。"""
        self._ensure_not_interrupted()
        try:
            target = self._resolve_path(path)
            self._check_path_safety(path)
        except ValueError as e:
            return False, str(e)

        self._observe_staging_path(target)

        is_new = not target.exists()

        # 检查暂存区：如果存在则写入暂存而非磁盘
        staging = self._get_staging_area()
        if staging:
            current_content: str | None = staging.get_staged_content(str(target))
            if current_content is None and target.exists():
                try:
                    async with aiofiles.open(target, "r", encoding="utf-8", errors="replace") as f:
                        current_content = await f.read()
                except OSError as e:
                    return False, f"读取文件失败: {e}"
            self._ensure_not_interrupted()
            is_new_staged = not target.exists() and not staging.has_staged(str(target))
            try:
                staged_action = staging.stage_write(str(target), content, is_new_staged)
            except OSError as e:
                return False, f"写入文件失败: {e}"

            # 创建 checkpoint 快照（暂存模式下也创建，用于回滚追踪）
            checkpoint_mgr = self._get_checkpoint_manager()
            checkpoint = None
            if checkpoint_mgr:
                agent_name = getattr(self, "agent_name", "coding_agent")
                checkpoint = await checkpoint_mgr.snapshot_before_write(
                    str(target), agent_name, f"write: {path}"
                )

            # 通知前端文件变更
            action = staged_action
            diff_text = self._build_diff(
                path,
                original_content=current_content,
                new_content=content,
            )
            await self._notify_file_change(path, action, diff_text, content=content if action == "create" else "")

            if checkpoint:
                await self._notify_checkpoint_created(checkpoint)

            action_label = "创建" if is_new_staged else "修改"
            line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            return True, f"[暂存] 已{action_label}文件 {path}（{line_count} 行，{len(content)} 字节）"

        # 无暂存区：走原有磁盘写入逻辑
        original_content: str | None = None
        if target.exists():
            try:
                async with aiofiles.open(target, "r", encoding="utf-8", errors="replace") as f:
                    original_content = await f.read()
            except OSError as e:
                return False, f"读取文件失败: {e}"

        # 创建 checkpoint 快照
        checkpoint_mgr = self._get_checkpoint_manager()
        checkpoint = None
        if checkpoint_mgr:
            agent_name = getattr(self, "agent_name", "coding_agent")
            checkpoint = await checkpoint_mgr.snapshot_before_write(
                str(target), agent_name, f"write: {path}"
            )

        # 创建父目录
        if create_dirs:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return False, f"创建目录失败: {e}"

        # 写入文件
        self._ensure_not_interrupted()
        try:
            async with aiofiles.open(target, "w", encoding="utf-8", newline="") as f:
                await f.write(content)
        except OSError as e:
            return False, f"写入文件失败: {e}"

        # 统计行数
        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)

        # 通知前端文件变更
        action = "create" if is_new else "modify"
        diff_text = self._build_diff(
            path,
            original_content=original_content,
            new_content=content,
        )
        await self._notify_file_change(path, action, diff_text, content=content if action == "create" else "")

        # 通知 checkpoint 创建
        if checkpoint:
            await self._notify_checkpoint_created(checkpoint)

        action_label = "创建" if is_new else "覆盖"
        return True, f"已{action_label}文件 {path}（{line_count} 行，{len(content)} 字节）"

    @staticmethod
    def _build_diff(path: str, original_content: str | None, new_content: str) -> str:
        """构建 write 操作的 unified diff。"""
        new_lines = new_content.splitlines(keepends=True)
        if original_content is None:
            diff_lines = list(difflib.unified_diff(
                [],
                new_lines,
                fromfile="/dev/null",
                tofile=f"b/{path}",
                lineterm="",
            ))
            return "".join(diff_lines) if diff_lines else "(空文件)"

        original_lines = original_content.splitlines(keepends=True)
        diff_lines = list(difflib.unified_diff(
            original_lines,
            new_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        ))
        return "".join(diff_lines) if diff_lines else "(无差异)"
