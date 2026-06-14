"""创建实施计划文档工具。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

import aiofiles
from src.app.plugin_system.base import BaseTool

from .base import CodingToolMixin


class CreatePlanTool(CodingToolMixin, BaseTool):
    """在工作目录的 .agents/context/ 下创建计划文档。"""

    tool_name = "create_plan"
    tool_description = (
        "Create a structured implementation plan document under .agents/context/. "
        "Use this to formalize the plan before handing it to implement_plan."
    )
    chatter_allow = ["coding_agent"]

    async def execute(
        self,
        title: Annotated[str, "Short descriptive title for the plan"],
        content: Annotated[str, "Full plan content in Markdown format"],
    ) -> tuple[bool, str]:
        """创建计划文档并返回路径。"""
        work_dir = Path(self._get_working_directory())
        context_dir = work_dir / ".agents" / "context"

        try:
            context_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return False, f"无法创建目录 .agents/context/: {e}"

        # 文件名：时间戳 + 标题
        safe_title = "".join(
            c if c.isalnum() or c in ("-", "_") else "_"
            for c in title.strip().replace(" ", "_")
        )[:60]
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_{safe_title}.md"
        plan_path = context_dir / filename

        try:
            full_content = f"# {title}\n\n> Created: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n{content}"
            async with aiofiles.open(plan_path, "w", encoding="utf-8", newline="") as f:
                await f.write(full_content)
        except OSError as e:
            return False, f"写入计划文档失败: {e}"

        relative_path = str(plan_path.relative_to(work_dir))

        # 通知前端
        await self._notify_file_change(relative_path, "create", content=full_content)

        return True, (
            f"计划文档已创建: {relative_path}\n"
            f"使用 implement_plan(plan_path=\"{relative_path}\") 交给 Coder Agent 实施。"
        )
