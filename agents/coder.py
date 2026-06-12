"""代码实施 Agent。"""

from __future__ import annotations

from typing import Annotated, Any

from src.app.plugin_system.base import BaseAgent
from src.core.config import get_model_config
from src.kernel.llm import (
    LLMContextManager,
    LLMPayload,
    LLMUsable,
    ModelSet,
    ROLE,
    ReminderSourceSpec,
    Text,
    ToolResult,
)
from ..config import CoderModelProfile

from ..context_compression import coding_context_compression_handler
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
        model_profile: Annotated[
            str,
            "可选：指定 Coder 模型 Profile 名称，如 claude-architect。留空使用默认 coding_coder 模型",
        ] = "",
    ) -> tuple[bool, str | dict]:
        """实施代码变更。

        支持通过 model_profile 参数选择不同的模型 Profile：
        - 有值时从 config.model_profiles 查找并构建 ModelSet
        - 无值时走原有 get_task("coding_coder") 逻辑

        项目上下文和关联项目信息已通过 code_coder reminder 槽位自动注入，
        无需显式传递。
        """
        model_set = self._resolve_model_set(model_profile)
        if isinstance(model_set, str):
            # _resolve_model_set 返回了错误消息字符串
            return False, model_set
        session = get_session_manager().get_session_by_stream_id(self.stream_id)
        session_id = session.id if session else ""

        # 创建请求，通过 context_manager 注入 code_coder 槽位和压缩处理器
        context_manager = LLMContextManager(
            context_compression_handler=coding_context_compression_handler,
            reminder_sources=[
                ReminderSourceSpec(
                    bucket="code_coder",
                    wrap_with_system_tag=True,
                )
            ],
        )
        request = self.create_llm_request(
            model_set,
            "coder",
            with_usables=True,
            context_manager=context_manager,
        )
        # 确保 stream_id 在 meta_data 中，供压缩处理器推导 session_id
        request.meta_data["stream_id"] = self.stream_id
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

            # 推送 coder 的上下文用量到前端
            await self._push_context_usage(session_id, response)

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

    async def _push_context_usage(self, session_id: str, response: Any) -> None:
        """推送 Coder 的 LLM 上下文用量到前端。"""
        if not session_id:
            return
        usage = getattr(response, "_usage", None)
        if not usage:
            return
        total_tokens = usage.get("total_tokens", 0)
        if not total_tokens:
            return
        max_context = 0
        model_name = ""
        cost = 0.0
        model_set = getattr(response, "model_set", None)
        if model_set and isinstance(model_set, list) and len(model_set) > 0:
            model_entry = model_set[0]
            if isinstance(model_entry, dict):
                max_context = model_entry.get("max_context", 0)
                model_name = model_entry.get("model_identifier", "")
                try:
                    from src.kernel.llm.observation import calculate_request_cost
                    cost = calculate_request_cost(model=model_entry, usage=usage)
                except Exception:
                    pass
        await get_session_manager().broadcast_to_session(session_id, {
            "type": "agent.context_usage",
            "payload": {
                "total_tokens": total_tokens,
                "max_context": max_context,
                "source": self._FRONTEND_SOURCE,
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "cache_hit_tokens": usage.get("cache_hit_tokens", 0),
                "model_name": model_name,
                "cost": cost,
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

    def _resolve_model_set(self, model_profile: str) -> ModelSet | str:
        """根据 model_profile 参数决议 ModelSet。

        Args:
            model_profile: Profile 名称，空字符串表示使用默认模型

        Returns:
            ModelSet（成功）或错误消息字符串（profile 无效）
        """
        if not model_profile:
            return get_model_config().get_task("coding_coder")

        config = getattr(self.plugin, "config", None)
        if config is None:
            return get_model_config().get_task("coding_coder")

        profiles: list[CoderModelProfile] = config.model_profiles
        if not profiles:
            return get_model_config().get_task("coding_coder")

        from ..services.model_router import ModelRouter

        router = ModelRouter(profiles)
        try:
            profile = router.get_profile(model_profile)
        except ValueError as e:
            return str(e)

        return router.build_model_set(profile)

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
