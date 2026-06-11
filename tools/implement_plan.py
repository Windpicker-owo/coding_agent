"""将计划交给 Coder Agent 实施的工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import aiofiles
from src.app.plugin_system.base import BaseTool

from .base import CodingToolMixin


class ImplementPlanTool(CodingToolMixin, BaseTool):
    """读取计划文档并交给 Coder Agent 执行实施。"""

    tool_name = "implement_plan"
    tool_description = (
        "Hand off a plan document to the Coder Agent for implementation. "
        "Accepts either a plan_path (relative to .agents/context/) or inline plan_content."
    )
    chatter_allow = ["coding_agent"]

    async def execute(
        self,
        plan_path: Annotated[str, "Path to the plan document created by create_plan"] = "",
        plan_content: Annotated[str, "Inline plan content (alternative to plan_path)"] = "",
    ) -> tuple[bool, str]:
        """实施计划。"""
        if not plan_path and not plan_content:
            return False, "必须提供 plan_path 或 plan_content 其中之一"

        # 读取计划内容
        content = plan_content
        if plan_path and not content:
            try:
                target = self._resolve_path(plan_path)
                self._check_path_safety(plan_path)
            except ValueError as e:
                return False, str(e)

            if not target.exists():
                return False, f"计划文档不存在: {plan_path}"

            try:
                async with aiofiles.open(target, "r", encoding="utf-8") as f:
                    content = await f.read()
            except OSError as e:
                return False, f"读取计划文档失败: {e}"

        # 获取项目上下文（从 system reminder 存储中）
        from src.core.prompt import get_system_reminder_store
        store = get_system_reminder_store()
        project_context = store.get(bucket="code_main_agent", names=["project_context"])

        # 调用 Coder Agent
        from ..agents.coder import CoderAgent

        session = self._get_current_session()
        if session is None:
            return False, "无法获取当前会话"

        plugin = getattr(self, "plugin", None)
        if plugin is None:
            return False, "无法获取插件实例"

        stream_id = getattr(self, "stream_id", "")
        coder = CoderAgent(stream_id=stream_id, plugin=plugin)

        try:
            success, result = await coder.execute(
                implementation_plan=content,
                project_context=project_context or "",
            )
        except Exception as e:
            return False, f"Coder Agent 执行失败: {e}"

        result_str = result if isinstance(result, str) else str(result)
        status = "成功" if success else "失败"
        return success, f"[Coder Agent {status}]\n{result_str}"
