"""文件创建/覆盖工具。"""

from __future__ import annotations

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
        try:
            target = self._resolve_path(path)
            self._check_path_safety(path)
        except ValueError as e:
            return False, str(e)

        is_new = not target.exists()

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
        try:
            async with aiofiles.open(target, "w", encoding="utf-8", newline="") as f:
                await f.write(content)
        except OSError as e:
            return False, f"写入文件失败: {e}"

        # 统计行数
        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)

        # 通知前端文件变更
        action = "create" if is_new else "modify"
        await self._notify_file_change(path, action)

        # 通知 checkpoint 创建
        if checkpoint:
            await self._notify_checkpoint_created(checkpoint)

        action_label = "创建" if is_new else "覆盖"
        return True, f"已{action_label}文件 {path}（{line_count} 行，{len(content)} 字节）"
