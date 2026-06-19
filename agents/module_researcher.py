"""模块研究 Agent。"""

from __future__ import annotations

from typing import Annotated

import json_repair
from src.app.plugin_system.base import BaseAgent
from src.app.plugin_system.api.prompt_api import get_template
from src.core.config import get_model_config
from src.kernel.llm import LLMPayload, LLMUsable, ROLE, Text, ToolResult

from ..mcp_integration import get_mcp_tools_for_agent
from ..tools import ReadTool, GrepTool, FindTool, LsTool
from ..prompts import build_environment_info
from ..services.terminal_environment import get_preferred_terminal_from_config


class ModuleResearcherAgent(BaseAgent):
    """深入研究指定代码模块。"""

    agent_name = "module_researcher"
    agent_description = "深入研究指定代码模块，输出结构化报告"
    chatter_allow = ["coding_agent"]
    associated_types = ["text"]
    usables = [ReadTool, GrepTool, FindTool, LsTool]  # 严格只读

    def _get_model_task(self, key: str, default: str) -> str:
        """从插件配置安全读取模型任务名。"""
        config = getattr(self.plugin, "config", None)
        if config is None:
            return default
        model = getattr(config, "model", None)
        if model is None:
            return default
        return getattr(model, key, default) or default

    def _get_extra_usables(self) -> list[type[LLMUsable]]:
        """注入 MCP 工具（researcher Agent）。"""
        return get_mcp_tools_for_agent(self.plugin, "researcher")

    async def execute(
        self,
        module_path: Annotated[str, "模块目录路径"],
        research_focus: Annotated[str, "研究重点描述"],
        gitignore_content: Annotated[str, "项目根目录 .gitignore 内容，可为空"] = "",
    ) -> tuple[bool, str | dict]:
        """执行模块研究。"""
        task_name = self._get_model_task("researcher_task", "coding_researcher")
        model_set = get_model_config().get_task(task_name)

        request = self.create_llm_request(
            model_set, "module_researcher",
            with_usables=True,
            with_reminder="code_researcher",
        )
        terminal_environment = get_preferred_terminal_from_config(
            getattr(self.plugin, "config", None)
        )

        tmpl = get_template("coding_agent.module_researcher")
        if tmpl is not None:
            tmpl.set("environment_info", build_environment_info(terminal_environment))
            system_prompt = await tmpl.build()
        else:
            system_prompt = ""

        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
        user_prompt = f"研究模块：{module_path}\n研究重点：{research_focus}"
        if gitignore_content.strip():
            user_prompt += (
                "\n以下是项目根目录 .gitignore 内容。禁止读取和分析其中忽略的路径：\n"
                f"```gitignore\n{gitignore_content}\n```"
            )
        request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))

        # 多轮工具调用循环
        while True:
            response = await request.send(stream=True)
            await response

            if response.call_list:
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
                request = response
            else:
                break

        message = getattr(response, "message", "") or ""
        try:
            parsed = json_repair.repair_json(message, return_objects=True)
            if isinstance(parsed, dict):
                return True, parsed
        except Exception:
            pass

        return True, message
