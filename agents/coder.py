"""代码实施 Agent。"""

from __future__ import annotations

from typing import Annotated, Any

from src.app.plugin_system.base import BaseAgent
from src.core.config import get_model_config
from src.app.plugin_system.api.prompt_api import add_system_reminder
from src.kernel.llm import LLMPayload, LLMUsable, ROLE, Text, ToolResult

from ..mcp_integration import get_mcp_tools_for_agent
from ..tools import BashTool, ReadTool, WriteTool, EditTool
from ..prompts import CODER_AGENT_PROMPT, render_prompt
from ..session_manager import get_session_manager
from ..services.terminal_environment import get_preferred_terminal_from_config


class CoderAgent(BaseAgent):
    """按照落地计划精确实施代码变更。"""

    agent_name = "coder"
    agent_description = "按照落地计划精确实施代码变更"
    chatter_allow = ["coding_agent"]
    associated_types = ["text"]
    usables = [BashTool, ReadTool, WriteTool, EditTool]  # 可写工具
    _FRONTEND_SOURCE = "coder"

    def _get_extra_usables(self) -> list[type[LLMUsable]]:
        """注入 MCP 工具（coder Agent）。"""
        return get_mcp_tools_for_agent(self.plugin, "coder")

    async def execute(
        self,
        implementation_plan: Annotated[str, "落地计划文档（Markdown）"],
        project_context: Annotated[str, "项目上下文摘要"],
    ) -> tuple[bool, str | dict]:
        """实施代码变更。"""
        model_set = get_model_config().get_task("coding_coder")
        session = get_session_manager().get_session_by_stream_id(self.stream_id)
        session_id = session.id if session else ""

        # 将项目上下文存入 system reminder 存储（fixed 模式，不截断）
        add_system_reminder(
            bucket="code_coder",
            name="project_context",
            content=f"<project_context>\n{project_context}\n</project_context>",
            insert_type="fixed",
            consume="forever",
        )

        # 注入 skills 目录（如果存在）
        from ..services.skill_loader import build_skills_catalog
        if session:
            catalog = build_skills_catalog(session.working_directory)
            if catalog:
                add_system_reminder(
                    bucket="code_coder",
                    name="skills",
                    content=catalog,
                    insert_type="fixed",
                    consume="forever",
                )

        # 创建请求，通过 with_reminder 注入 code_coder 槽位
        request = self.create_llm_request(
            model_set,
            "coder",
            with_usables=True,
            with_reminder="code_coder",
        )
        terminal_environment = get_preferred_terminal_from_config(
            getattr(self.plugin, "config", None)
        )

        # System prompt 包含落地计划（不含项目上下文）
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(
            render_prompt(CODER_AGENT_PROMPT, terminal_environment=terminal_environment)
        )))
        request.add_payload(LLMPayload(ROLE.USER, Text(
            "请根据以下落地计划开始实施，并严格限制在计划范围内：\n"
            f"<implementation_plan>\n{implementation_plan}\n</implementation_plan>"
        )))

        # 多轮工具调用循环
        await self._notify_status(session_id, "coding", "Coder 正在读取计划并开始实施...")
        while True:
            response = await request.send(stream=True)

            last_reasoning = ""
            current_tool_call_id = ""
            announced_tool_calls: set[str] = set()
            async for event in response.stream_events():
                chunk = event.text_delta or ""
                if chunk:
                    await self._stream_to_frontend(session_id, chunk)

                if event.tool_call_id:
                    current_tool_call_id = event.tool_call_id
                effective_tool_call_id = event.tool_call_id or current_tool_call_id
                if (
                    event.tool_name
                    and effective_tool_call_id
                    and effective_tool_call_id not in announced_tool_calls
                ):
                    announced_tool_calls.add(effective_tool_call_id)
                    await self._notify_tool_call(
                        session_id,
                        event.tool_name,
                        {},
                        stage="planning",
                    )

                reasoning = response.reasoning_content or ""
                if reasoning and reasoning != last_reasoning:
                    last_reasoning = reasoning
                    await self._stream_thinking_to_frontend(session_id, reasoning)

            if response.reasoning_content:
                await self._stream_thinking_to_frontend(
                    session_id,
                    response.reasoning_content,
                )

            if response.call_list:
                await self._notify_status(session_id, "coding", "Coder 正在执行实现工具...")
                for call in response.call_list:
                    call_args = call.args if isinstance(call.args, dict) else {}
                    await self._notify_tool_call(
                        session_id,
                        call.name,
                        call_args,
                        stage="running",
                    )
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
                # 工具轮次间检查 chatter 转发的 guidance 队列
                if session and session.coder_guidance_queue:
                    guidance_texts: list[str] = []
                    while session.coder_guidance_queue:
                        guidance_texts.append(session.coder_guidance_queue.pop(0))
                    combined = "\n".join(guidance_texts)
                    response.add_payload(LLMPayload(ROLE.USER, Text(combined)))
                    await self._notify_status(session_id, "coding", "已接收引导消息")

                request = response
            else:
                break

        message = getattr(response, "message", "") or ""
        await self._stream_final(session_id)
        return True, message

    async def _notify_status(self, session_id: str, phase: str, detail: str) -> None:
        if not session_id:
            return
        await get_session_manager().broadcast_to_session(session_id, {
            "type": "agent.status",
            "payload": {
                "phase": phase,
                "detail": detail,
                "source": self._FRONTEND_SOURCE,
            },
        })

    async def _stream_to_frontend(self, session_id: str, chunk: str) -> None:
        if not session_id:
            return
        await get_session_manager().broadcast_to_session(session_id, {
            "type": "agent.text",
            "payload": {
                "content": chunk,
                "is_final": False,
                "source": self._FRONTEND_SOURCE,
            },
        })

    async def _stream_thinking_to_frontend(self, session_id: str, content: str) -> None:
        if not session_id:
            return
        await get_session_manager().broadcast_to_session(session_id, {
            "type": "agent.thinking",
            "payload": {
                "content": content,
                "source": self._FRONTEND_SOURCE,
            },
        })

    async def _notify_tool_call(
        self,
        session_id: str,
        name: str,
        args: dict[str, Any],
        stage: str = "running",
    ) -> None:
        if not session_id:
            return
        await get_session_manager().broadcast_to_session(session_id, {
            "type": "tool.call",
            "payload": {
                "name": name,
                "args_summary": self._summarize_args(args),
                "source": self._FRONTEND_SOURCE,
                "stage": stage,
            },
        })

    async def _stream_final(self, session_id: str) -> None:
        if not session_id:
            return
        await get_session_manager().broadcast_to_session(session_id, {
            "type": "agent.text",
            "payload": {
                "content": "",
                "is_final": True,
                "source": self._FRONTEND_SOURCE,
            },
        })

    @staticmethod
    def _summarize_args(args: dict[str, Any]) -> str:
        if not args:
            return ""
        parts: list[str] = []
        for key, value in args.items():
            value_text = str(value)
            if len(value_text) > 60:
                value_text = value_text[:57] + "..."
            parts.append(f"{key}={value_text!r}")
        summary = ", ".join(parts)
        if len(summary) > 120:
            summary = summary[:117] + "..."
        return summary
