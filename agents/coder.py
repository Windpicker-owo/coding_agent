"""代码实施 Agent。"""

from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any

from src.app.plugin_system.base import BaseAgent
from src.app.plugin_system.api.prompt_api import get_template
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
from ..prompts import build_environment_info
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
    _AUTO_SAVE_MIN_INTERVAL: float = 3.0  # 防抖自动保存最小间隔秒数
    _FINAL_GUIDANCE_GRACE_SECONDS: float = 0.35  # 收尾前短暂等待追加引导转发

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
        task_observer = (
            get_session_manager().build_runtime_task_observer(session_id)
            if session_id
            else None
        )

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
        tmpl = get_template("coding_agent.coder")
        if tmpl is not None:
            tmpl.set("environment_info", build_environment_info(terminal_environment))
            system_prompt = await tmpl.build()
        else:
            system_prompt = ""

        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
        request.add_payload(LLMPayload(ROLE.USER, Text(
            "请根据以下落地计划开始实施，并严格限制在计划范围内：\n"
            f"<implementation_plan>\n{implementation_plan}\n</implementation_plan>"
        )))

        # 多轮工具调用循环
        await self._notify_status(session_id, "coding", "Coder 正在读取计划并开始实施...")

        # 创建文件追踪暂存区：write/edit 会立即落到磁盘，但保留初始内容用于最终 diff / 回滚
        from ..services.file_staging import FileStagingArea
        staging = None
        if session:
            staging = FileStagingArea(
                session.working_directory,
                linked_directories=session.linked_directories,
            )
            session.staging_area = staging

        try:
            last_auto_save_time: float = 0.0
            while True:
                response = await request.send(stream=True)

                last_reasoning = ""
                reasoning_emitted = False
                async for event in response.stream_events():
                    chunk = event.text_delta or ""
                    if chunk:
                        await self._stream_to_frontend(session_id, chunk)

                    reasoning = response.reasoning_content or ""
                    if reasoning and reasoning != last_reasoning:
                        last_reasoning = reasoning
                        reasoning_emitted = True
                        await self._stream_thinking_to_frontend(session_id, reasoning)

                if response.reasoning_content and not reasoning_emitted:
                    await self._stream_thinking_to_frontend(
                        session_id,
                        response.reasoning_content,
                    )

                # 推送 coder 的上下文用量到前端
                await self._push_context_usage(session_id, response)

                if response.call_list:
                    # 在开始调用工具前，先终结当前轮次的文本流
                    await self._stream_final(session_id)
                    await self._notify_status(session_id, "coding", "Coder 正在执行工具...")
                    for call in response.call_list:
                        call_args = call.args if isinstance(call.args, dict) else {}
                        await self._notify_tool_call(
                            session_id,
                            call.name,
                            call_args,
                            call_id=call.id,
                            stage="running",
                        )
                        try:
                            success, result = await self.execute_local_usable(
                                call.name,
                                task_observer=task_observer,
                                **call_args,
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
                    await self._apply_pending_guidance(
                        session,
                        response,
                        session_id,
                        detail="已接收引导消息",
                    )

                    request = response

                    # 防抖自动保存：将 Coder 的 payloads 序列化到 session，供 Main Agent 保存
                    now = time.time()
                    if now - last_auto_save_time >= self._AUTO_SAVE_MIN_INTERVAL:
                        last_auto_save_time = now
                        await self._save_coder_progress(session, request)
                else:
                    # 给 guidance 转发后台任务留一个极短窗口，避免“最后一刻发来的引导”丢在收尾阶段
                    if session is not None:
                        await asyncio.sleep(self._FINAL_GUIDANCE_GRACE_SECONDS)
                    has_late_guidance = await self._apply_pending_guidance(
                        session,
                        response,
                        session_id,
                        detail="已接收新的引导消息，正在继续调整结果...",
                    )
                    if has_late_guidance:
                        await self._stream_final(session_id)
                        request = response
                        now = time.time()
                        if now - last_auto_save_time >= self._AUTO_SAVE_MIN_INTERVAL:
                            last_auto_save_time = now
                            await self._save_coder_progress(session, request)
                        continue
                    break

            # Coder 成功完成：commit 暂存区变更到磁盘
            commit_result = None
            if staging:
                commit_result = await staging.commit()

            # 清除中间状态
            if session:
                session._coder_payloads_data = None
                session.staging_area = None

            message = getattr(response, "message", "") or ""
            await self._stream_final(session_id)

            # 返回结果包含文件列表与整体 diff，供 Main Agent 继续总结/汇报
            if commit_result and commit_result.files_changed:
                sections: list[str] = []
                if message.strip():
                    sections.append(message.strip())
                if commit_result.summary.strip():
                    sections.append(f"## 变更摘要\n{commit_result.summary.strip()}")
                if commit_result.combined_diff.strip():
                    sections.append(
                        "## 总体 Diff\n```diff\n"
                        f"{commit_result.combined_diff.strip()}\n"
                        "```"
                    )
                return True, "\n\n".join(section for section in sections if section.strip())
            return True, message

        except BaseException:
            # Coder 失败/异常/取消：rollback 暂存区，不留半成品
            if staging:
                staging.rollback()
            if session:
                session.staging_area = None
                session._coder_payloads_data = None
            raise

    async def _notify_status(self, session_id: str, phase: str, detail: str) -> None:
        if not session_id:
            return
        session_mgr = get_session_manager()
        session = session_mgr.get_session(session_id)
        if session is not None:
            session.phase = phase
        await session_mgr.broadcast_to_session(session_id, {
            "type": "agent.status",
            "payload": {
                "phase": phase,
                "detail": detail,
                "source": self._FRONTEND_SOURCE,
            },
        })

    async def _apply_pending_guidance(
        self,
        session: Any,
        request: Any,
        session_id: str,
        *,
        detail: str,
    ) -> bool:
        """将 chatter 转发来的工作中引导注入到当前请求链。"""
        if session is None or not session.coder_guidance_queue:
            return False

        guidance_texts: list[str] = []
        while session.coder_guidance_queue:
            guidance_texts.append(session.coder_guidance_queue.pop(0))

        if not guidance_texts:
            return False

        request.add_payload(LLMPayload(ROLE.USER, Text("\n".join(guidance_texts))))
        await self._notify_status(session_id, "coding", detail)
        return True

    async def _save_coder_progress(self, session: Any, request: Any) -> None:
        """保存 Coder Agent 的当前进度到 session 中间字段。

        将 Coder 的 payloads 序列化到 session._coder_payloads_data，
        由 Main Agent 的 _auto_save 一并持久化到磁盘。
        """
        if session is None:
            return
        try:
            from ..session_store import serialize_payload
            session._coder_payloads_data = [serialize_payload(p) for p in request.payloads]
        except Exception:
            pass

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
        session_mgr = get_session_manager()
        session = session_mgr.get_session(session_id)
        if session and session.session_store:
            asyncio.ensure_future(session_mgr.persist_session_metadata(session_id))
        await session_mgr.broadcast_to_session(session_id, {
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
        call_id: str = "",
        stage: str = "running",
    ) -> None:
        if not session_id:
            return
        await get_session_manager().broadcast_to_session(session_id, {
            "type": "tool.call",
            "payload": {
                "call_id": call_id,
                "name": name,
                "args": args,
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
        coder_task = self._get_model_task("coder_task", "coding_coder")
        if not model_profile:
            return get_model_config().get_task(coder_task)

        config = getattr(self.plugin, "config", None)
        if config is None:
            return get_model_config().get_task(coder_task)

        profiles: list[CoderModelProfile] = config.model_profiles
        if not profiles:
            return get_model_config().get_task(coder_task)

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
