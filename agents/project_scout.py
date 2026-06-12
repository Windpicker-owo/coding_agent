"""项目侦察 Agent。"""

from __future__ import annotations

from typing import Annotated

import json_repair
from src.app.plugin_system.base import BaseAgent
from src.core.config import get_model_config
from src.kernel.llm import LLMPayload, LLMUsable, ROLE, Text, ToolResult

from ..mcp_integration import get_mcp_tools_for_agent
from ..tools import ReadTool, LsTool, FindTool, GrepTool
from ..prompts import PROJECT_SCOUT_PROMPT, render_prompt
from ..services.terminal_environment import get_preferred_terminal_from_config


class ProjectScoutAgent(BaseAgent):
    """快速侦察项目结构。"""

    agent_name = "project_scout"
    agent_description = "快速侦察项目结构，识别模块边界和技术栈"
    chatter_allow = ["coding_agent"]
    associated_types = ["text"]
    usables = [ReadTool, LsTool, FindTool, GrepTool]  # 严格只读

    def _get_extra_usables(self) -> list[type[LLMUsable]]:
        """注入 MCP 工具（researcher Agent）。"""
        return get_mcp_tools_for_agent(self.plugin, "researcher")

    async def execute(
        self,
        project_root: Annotated[str, "项目根目录路径"],
        gitignore_content: Annotated[str, "项目根目录 .gitignore 内容，可为空"] = "",
    ) -> tuple[bool, str | dict]:
        """执行项目侦察。"""
        # 获取模型配置
        model_set = get_model_config().get_task("coding_researcher")

        # 创建 LLM 请求
        request = self.create_llm_request(
            model_set, "project_scout",
            with_usables=True,
            with_reminder="code_scout",
        )
        terminal_environment = get_preferred_terminal_from_config(
            getattr(self.plugin, "config", None)
        )

        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(
            render_prompt(PROJECT_SCOUT_PROMPT, terminal_environment=terminal_environment)
        )))
        user_prompt = f"侦察项目：{project_root}"
        if gitignore_content.strip():
            user_prompt += (
                "\n以下是项目根目录 .gitignore 内容。你必须先理解这些规则，并在侦察中忽略所有匹配路径：\n"
                f"```gitignore\n{gitignore_content}\n```"
            )
        request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))

        # 多轮工具调用循环
        max_iterations = 20
        for _ in range(max_iterations):
            response = await request.send(stream=False)
            await response

            if response.call_list:
                # 执行工具调用
                for call in response.call_list:
                    call_args = call.args if isinstance(call.args, dict) else {}
                    try:
                        success, result = await self.execute_local_usable(
                            call.name, **call_args
                        )
                        result_str = str(result) if not isinstance(result, str) else result
                        response.add_payload(LLMPayload(
                            ROLE.TOOL_RESULT,
                            ToolResult(value=result_str, call_id=call.id),
                        ))
                    except Exception as e:
                        response.add_payload(LLMPayload(
                            ROLE.TOOL_RESULT,
                            ToolResult(value=f"错误: {e}", call_id=call.id),
                        ))
                # 继续下一轮
                request = response
            else:
                # 没有工具调用，agent 返回了最终结果
                break

        # 解析结果为 JSON
        message = getattr(response, "message", "") or ""
        try:
            parsed = json_repair.repair_json(message, return_objects=True)
            if isinstance(parsed, dict):
                return True, parsed
        except Exception:
            pass

        return True, message
