"""Coding Agent 主编排器。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator, cast

from src.app.plugin_system.base import (
    BaseChatter, Wait, WaitResumeEvent, Success, Failure, Stop,
)
from src.app.plugin_system.types import ChatType

from src.app.plugin_system.api.prompt_api import add_system_reminder
from src.kernel.llm import LLMPayload, LLMRequest, ROLE, Text, ToolResult, ToolRegistry
from src.kernel.logger import get_logger, COLOR

from .config import CodingAgentConfig
from .orchestration import CodingOrchestrator
from .prompts import MAIN_AGENT_SYSTEM_PROMPT, render_prompt
from .session_manager import get_session_manager
from .session_store import deserialize_payload, serialize_payload
from .mcp_integration import get_mcp_tools_for_agent
from .services.gitignore_scope import GitIgnoreScope
from .services.project_context import ProjectContextService
from .services.terminal_environment import get_preferred_terminal_from_config
from .services.tool_loop_guard import advance_silent_tool_rounds
from .tools import (
    BashTool, ReadTool, WriteTool, EditTool,
    CreatePlanTool, ImplementPlanTool,
)

logger = get_logger("coding_agent.chatter", display="CodingAgent", color=COLOR.YELLOW)


class CodingAgentChatter(BaseChatter):
    """编程智能体主编排器。"""

    chatter_name = "coding_agent"
    chatter_description = "编程智能体主编排器"
    allow_message_buffer = False
    stream_tick_interval = 0.5
    associated_platforms = ["coding_agent"]
    chat_type = ChatType.PRIVATE
    _MAX_SILENT_TOOL_ROUNDS = 3

    def __init__(self, stream_id: str, plugin: Any) -> None:
        super().__init__(stream_id, plugin)
        self._current_request: Any = None  # 当前 LLMRequest/LLMResponse，用于自动保存
        self._is_first_message: bool = True  # 是否首条用户消息（用于标题生成）
        self._message_count: int = 0  # 已处理的消息数
        self._goal_review_pending: bool = False
        self._last_trigger_msgs: list = []  # 最近一次有效触发消息，goal 审查时 fallback
        self._is_goal_review_round: bool = False  # 当前轮次是否为 goal 审查轮
        self._goal_round_modified: bool = False  # goal 审查本轮是否使用了修改性工具

    async def execute(self) -> AsyncGenerator[
        Wait | Success | Failure | Stop,
        WaitResumeEvent | None,
    ]:
        """主执行流程。"""
        session_mgr = get_session_manager()
        session = session_mgr.get_session_by_stream_id(self.stream_id)

        if session is None:
            yield Failure("无法获取 Coding Agent 会话")
            return

        project_root = session.working_directory

        # 绑定 stream_id
        session_mgr.bind_stream_id(session.id, self.stream_id)

        # ── 检查是否为恢复模式 ──
        if session._resume_payloads is not None:
            # 恢复模式：跳过项目研究，直接进入交互循环
            logger.info(f"会话 {session.id[:8]} 进入恢复模式，"
                        f"历史消息 {len(session._resume_payloads)} 条")

            project_context = session._resume_project_context
            current, registry = self._build_clean_request(project_context)

            # 回放历史 payloads（跳过 SYSTEM 和 TOOL，_build_clean_request 已生成）
            # 同时统计消息数
            history_count = 0
            for pdata in session._resume_payloads:
                if pdata["role"] not in ("system", "tool"):
                    payload = deserialize_payload(pdata)
                    current.add_payload(payload)
                    if pdata["role"] == "user":
                        history_count += 1

            self._message_count = history_count
            self._is_first_message = False  # 恢复的会话不是首次消息

            # 清理恢复数据，但保留 project_context 供后续 _auto_save 使用
            session._resume_payloads = None

            self._current_request = current
            await self._notify_status("ready", "会话已恢复，等待输入")

            async for event in self._interaction_loop(
                current, registry, session.id,
            ):
                yield event
            return

        # ── 正常模式：Phase 1 项目研究 ──
        context_svc = ProjectContextService(self.plugin)

        config = cast(CodingAgentConfig | None, getattr(self.plugin, "config", None))

        ttl = 24
        if config and hasattr(config, "context"):
            ttl = getattr(config.context, "cache_ttl_hours", 24)

        project_context: dict | None = None
        if not await context_svc.is_context_stale(project_root, ttl):
            project_context = await context_svc.load_context(project_root)

        if project_context is None:
            await self._notify_status("researching", "正在了解项目...")
            project_context = await self._run_project_research(project_root)
            await context_svc.save_context(project_root, project_context)

        # Phase 2：构建初始请求
        current, registry = self._build_clean_request(project_context)
        self._current_request = current

        # 存储 project_context 到 session，供 _auto_save 持久化使用
        session._resume_project_context = project_context

        await self._notify_status("ready", "项目已就绪")

        # Phase 3：用户交互循环
        async for event in self._interaction_loop(
            current, registry, session.id,
        ):
            yield event

    # ── 交互循环 ───────────────────────────────────────────

    async def _interaction_loop(
        self, current: Any, registry: ToolRegistry, session_id: str,
    ) -> AsyncGenerator[Wait | Success | Failure | Stop, WaitResumeEvent | None]:
        """用户交互循环，支持正常和恢复两种入口。

        每次迭代处理一条用户消息：发送 LLM 请求、流式推送响应、
        处理工具调用、自动保存、标题生成。
        """
        session_mgr = get_session_manager()

        while True:
            # ── Goal 模式自动审查 ──
            session = session_mgr.get_session(session_id)
            if session and session.goal_mode and self._goal_review_pending:
                # 先检查用户是否有新消息（中断审查循环）
                unreads_text, unreads = await self.fetch_unreads()
                if unreads:
                    self._goal_review_pending = False
                    await self.flush_unreads(unreads)
                    current.add_payload(LLMPayload(ROLE.USER, Text(unreads_text)))
                    self._is_first_message = False
                    self._message_count += 1
                    is_first = False
                    await self._notify_status("thinking", "正在分析...")
                else:
                    self._goal_review_pending = False
                    self._is_goal_review_round = True
                    self._goal_round_modified = False  # 重置本轮修改标记

                    # 构建全新的干净上下文，不受前面对话影响
                    current = self.create_request(
                        task="coding_main",
                        request_name="coding_goal_review",
                        with_reminder="code_main_agent",
                    )
                    current.add_payload(LLMPayload(ROLE.SYSTEM, Text(self._build_system_prompt())))
                    current.add_payload(LLMPayload(ROLE.USER, Text(
                        self._build_goal_review_prompt(session.goal_text)
                    )))

                    # 注册工具
                    registry = ToolRegistry()
                    for tool_cls in [
                        BashTool, ReadTool, WriteTool, EditTool,
                        CreatePlanTool, ImplementPlanTool,
                    ]:
                        registry.register(tool_cls)
                    for tool_cls in get_mcp_tools_for_agent(self.plugin, "main"):
                        registry.register(tool_cls)
                    tool_schemas = registry.get_all()
                    if tool_schemas:
                        current.add_payload(LLMPayload(ROLE.TOOL, tool_schemas))  # type: ignore[arg-type]

                    self._is_first_message = False
                    self._message_count += 1
                    is_first = False
                    await self._notify_status("thinking", "正在审查目标达成情况...")
                # 跳过原有 fetch_unreads，直接进入 LLM 处理
            else:
                unreads_text, unreads = await self.fetch_unreads()

                if not unreads:
                    yield Wait()  # type: ignore[misc]
                    continue

                await self.flush_unreads(unreads)

                # 保存触发消息，供 goal 审查时工具调用 fallback
                self._last_trigger_msgs = unreads

                # 记录是否为首条消息（用于标题生成）
                is_first = self._is_first_message
                self._is_first_message = False
                self._message_count += 1

                # 添加用户消息
                current.add_payload(LLMPayload(ROLE.USER, Text(unreads_text)))
                await self._notify_status("thinking", "正在分析...")

            # 发送请求
            try:
                current = await current.send(stream=True)

                # 流式推送到前端
                chunk_count = 0
                last_reasoning = ""
                current_tool_call_id = ""
                announced_tool_calls: set[str] = set()
                async for event in current.stream_events():
                    chunk = event.text_delta or ""
                    if chunk:
                        chunk_count += 1
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
                        await session_mgr.broadcast_to_session(session_id, {
                            "type": "tool.call",
                            "payload": {
                                "name": event.tool_name,
                                "args_summary": "",
                                "stage": "planning",
                            },
                        })

                    reasoning = current.reasoning_content or ""
                    if reasoning and reasoning != last_reasoning:
                        last_reasoning = reasoning
                        await self._stream_thinking_to_frontend(session_id, reasoning)

                logger.debug(
                    f"LLM 响应完成: chunks={chunk_count}, "
                    f"message={current.message!r:.80}, "
                    f"tool_calls={len(current.call_list) if current.call_list else 0}, "
                    f"has_thinking={bool(current.reasoning_content)}"
                )

                # 推送上下文用量到前端
                await self._push_context_usage(session_id, current)
            except Exception as e:
                logger.error(f"LLM 调用失败: {e}", exc_info=True)
                await self._notify_status("error", f"LLM 调用失败: {e}")
                await session_mgr.broadcast_to_session(session_id, {
                    "type": "agent.text",
                    "payload": {"content": current.message or "", "is_final": True},
                })
                # 恢复 current 指针，重置 goal 审查状态
                current = self._current_request
                self._goal_review_pending = False
                continue

            # 发送 thinking 内容（如有）
            if current.reasoning_content:
                await self._stream_thinking_to_frontend(
                    session_id,
                    current.reasoning_content,
                )

            # 条件性发送 is_final：仅当无工具调用时才结束
            if not current.call_list:
                # 若模型只返回了 thinking 没有 text，把 thinking 末尾作为可见输出
                final_content = ""
                if current.message:
                    # 取 thinking 最后 500 字符作为摘要
                    final_content = current.message
                elif current.reasoning_content:
                    rc = current.reasoning_content.strip()
                    final_content = rc[-500:] if len(rc) > 500 else rc
                await session_mgr.broadcast_to_session(session_id, {
                    "type": "agent.text",
                    "payload": {"content": final_content, "is_final": True},
                })
            else:
                # 有工具调用 → 处理工具调用（可能多轮）
                try:
                    await self._notify_status("coding", "正在执行工具...")
                    # goal 审查时 unreads 可能为空，用上次有效消息作 fallback
                    trigger_msgs = unreads if unreads else self._last_trigger_msgs
                    current = await self._handle_tool_calls(current, registry, trigger_msgs, session_id)
                    if current:
                        if current.reasoning_content:
                            await self._stream_thinking_to_frontend(
                                session_id,
                                current.reasoning_content,
                            )
                        await session_mgr.broadcast_to_session(session_id, {
                            "type": "agent.text",
                            "payload": {"content": current.message or "", "is_final": True},
                        })
                except Exception as e:
                    logger.error(f"工具调用后续处理失败: {e}", exc_info=True)
                    await self._notify_status("error", f"工具调用后续处理失败: {e}")
                    await session_mgr.broadcast_to_session(session_id, {
                        "type": "agent.text",
                        "payload": {
                            "content": "\n当前回合已中止：工具调用后未能恢复到可见响应。你可以发送补充引导后重试。",
                            "is_final": True,
                        },
                    })
                    current = self._current_request
                    self._goal_review_pending = False
                    continue

            # 更新 current_request 指针（goal 审查轮不更新，保持正常对话指针）
            was_goal_review = self._is_goal_review_round
            self._is_goal_review_round = False
            if not was_goal_review:
                self._current_request = current
                # 自动保存会话状态（fire-and-forget）
                asyncio.create_task(self._auto_save(session_id))

            # 首条消息后生成标题（fire-and-forget）
            if is_first:
                asyncio.create_task(self._generate_title(session_id, unreads_text))

            # ── Goal 模式完成检查 ──
            session = session_mgr.get_session(session_id)
            if session and session.goal_mode:
                current_msg = (current.message or "").strip()
                if "GOAL COMPLETE" in current_msg:
                    # 强制检查：如果本轮使用了修改性工具，GOAL COMPLETE 无效
                    if self._goal_round_modified:
                        logger.debug("Goal 审查轮有修改操作，GOAL COMPLETE 无效，继续审查")
                        self._goal_round_modified = False
                        self._goal_review_pending = True
                        continue
                    session.goal_mode = False
                    session.goal_text = ""
                    await session_mgr.broadcast_to_session(session_id, {
                        "type": "goal.complete", "payload": {},
                    })
                    await self._notify_status("ready", "目标已完成")
                else:
                    self._goal_review_pending = True
                    # goal 审查轮不更新 current_request
                    if not was_goal_review:
                        self._current_request = current
                    continue
            else:
                await self._notify_status("ready", "等待输入")

    # ── 自动保存 ───────────────────────────────────────────

    async def _auto_save(self, session_id: str) -> None:
        """异步保存当前会话状态到磁盘（fire-and-forget）。"""
        session_mgr = get_session_manager()
        session = session_mgr.get_session(session_id)
        if not session or not session.session_store:
            return

        request = self._current_request
        if request is None:
            return

        try:
            # 保存清洁 payloads：剥离已注入的 system_reminder 前缀
            clean_payloads = []
            for p in request.payloads:
                if p.role == ROLE.USER:
                    clean_content = self._strip_reminder_prefix(p.content)
                    clean_payloads.append(LLMPayload(ROLE.USER, clean_content))
                else:
                    clean_payloads.append(p)
            
            payloads_data = [serialize_payload(p) for p in clean_payloads]
            await session_mgr.save_session_state(
                session_id=session_id,
                payloads=payloads_data,
                project_context=session._resume_project_context,  # 显式传递
                message_count=self._message_count,
                linked_directories=session.linked_directories,
            )
        except Exception:
            logger.exception("自动保存会话失败")

    def _strip_reminder_prefix(self, content: list) -> list:
        """从 content 列表中移除 <system_reminder>...</system_reminder> 的 Text 块。
        
        对于只包含 system_reminder 的 Text 块，整块移除。
        对于混合了 system_reminder 和其他内容的 Text 块，仅移除 system_reminder 部分。
        
        Args:
            content: LLMPayload 的 content 列表
            
        Returns:
            移除 system_reminder 后的 content 列表
        """
        import re
        
        pattern = re.compile(r'<system_reminder>.*?</system_reminder>', re.DOTALL | re.MULTILINE)
        
        result = []
        for item in content:
            if not isinstance(item, Text):
                result.append(item)
                continue
            
            new_text = pattern.sub('', item.text).strip()
            if new_text:
                result.append(Text(new_text))
        
        return result

    # ── 标题生成 ───────────────────────────────────────────

    async def _generate_title(self, session_id: str, user_message: str) -> None:
        """使用小模型根据首条用户消息生成会话标题（不超过15字）。"""
        try:
            title_request = self.create_request(
                task="coding_title",
                request_name="coding_title",
            )

            prompt = (
                "根据以下用户消息生成一个简洁的会话标题，不超过15个字。"
                "只输出标题文本，不要加引号、标点或任何额外说明。\n\n"
                "用户消息：" + user_message
            )
            title_request.add_payload(LLMPayload(ROLE.SYSTEM, Text(prompt)))
            title_request.add_payload(LLMPayload(ROLE.USER, Text("请生成标题")))

            response = await title_request.send(stream=False)
            title = (response.message or "").strip()[:30]
            # 清理可能的引号和多余空白
            title = title.strip('"\'「」『』 \t\n\r')

            if title:
                session_mgr = get_session_manager()
                await session_mgr.update_session_title(session_id, title)
                logger.info(f"会话 {session_id[:8]} 标题已生成: {title!r}")
        except Exception:
            logger.exception("生成会话标题失败")

    async def _run_project_research(self, project_root: str) -> dict:
        """执行完整的项目研究流程。

        委托给 CodingOrchestrator.run_full_research，自身仅负责：
        - 读取 gitignore 并提前显示状态通知
        - 包装 progress_callback 以广播 research.progress 到前端
        - 发送研究完成通知
        """
        orchestrator = CodingOrchestrator(self.plugin, self.stream_id)
        gitignore_scope = GitIgnoreScope.load(project_root)

        async def _broadcast_research_progress(payload: dict[str, Any]) -> None:
            session_mgr = get_session_manager()
            session = session_mgr.get_session_by_stream_id(self.stream_id)
            if session is None:
                return
            payload.setdefault("ignored_patterns_count", len(gitignore_scope.rules))
            payload.setdefault(
                "scope_summary",
                "仅研究未被 .gitignore 忽略的路径" if gitignore_scope.rules else "研究全部项目路径",
            )
            await session_mgr.broadcast_to_session(session.id, {
                "type": "research.progress",
                "payload": payload,
            })

        if gitignore_scope.rules:
            await self._notify_status(
                "researching",
                f"已读取 .gitignore，检测到 {len(gitignore_scope.rules)} 条忽略规则，正在侦察项目结构...",
            )
        else:
            await self._notify_status("researching", "未发现 .gitignore，正在侦察项目结构...")

        # 发送初始进度（让前端知道研究已开始）
        await _broadcast_research_progress({
            "total": 0,
            "completed": 0,
            "current_module": "准备开始项目侦察",
            "active_agents": [],
        })

        # 调用完整研究
        result = await orchestrator.run_full_research(
            project_root,
            gitignore_content=gitignore_scope.raw_content,
            progress_callback=_broadcast_research_progress,
        )

        # 通知完成
        modules_count = len(result.get("modules", []))
        await _broadcast_research_progress({
            "total": modules_count,
            "completed": modules_count,
            "current_module": "研究完成",
            "active_agents": [],
        })

        return result

    def _build_clean_request(self, project_context: dict | None) -> tuple[LLMRequest, ToolRegistry]:
        """构建干净的主 agent 请求。"""
        # 只有当 project_context 有效时才设置 reminder
        if project_context and isinstance(project_context, dict):
            ctx_text = json.dumps(project_context, indent=2, ensure_ascii=False)
            reminder_content = f"<project_context>\n{ctx_text}\n</project_context>"
            # Main Agent 槽位
            add_system_reminder(
                bucket="code_main_agent",
                name="project_context",
                content=reminder_content,
                insert_type="fixed",
                consume="forever",
            )
            # Coder Agent 槽位，Coder 通过 with_reminder="code_coder" 自动获取
            add_system_reminder(
                bucket="code_coder",
                name="project_context",
                content=reminder_content,
                insert_type="fixed",
                consume="forever",
            )
        else:
            # 移除可能存在的旧 reminder（如果是恢复路径且 project_context 为 None）
            from src.core.prompt import get_system_reminder_store
            store = get_system_reminder_store()
            store.delete(bucket="code_main_agent", name="project_context")
            store.delete(bucket="code_coder", name="project_context")
            logger.warning("project_context 为空，已移除 system_reminder 注入")

        # 注入 skills 目录（如果存在）
        from .services.skill_loader import build_skills_catalog
        session = get_session_manager().get_session_by_stream_id(self.stream_id)
        if session:
            catalog = build_skills_catalog(session.working_directory)
            if catalog:
                add_system_reminder(
                    bucket="code_main_agent",
                    name="skills",
                    content=catalog,
                    insert_type="fixed",
                    consume="forever",
                )
                add_system_reminder(
                    bucket="code_coder",
                    name="skills",
                    content=catalog,
                    insert_type="fixed",
                    consume="forever",
                )

        # 构建请求，通过 with_reminder 注入 system reminder 槽位
        request = self.create_request(
            task="coding_main",
            request_name="coding_agent",
            with_reminder="code_main_agent",
        )

        # 填充人格信息
        system_prompt = self._build_system_prompt()
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))

        # 注入工具
        registry = ToolRegistry()
        for tool_cls in [
            BashTool, ReadTool, WriteTool, EditTool,
            CreatePlanTool, ImplementPlanTool,
        ]:
            registry.register(tool_cls)

        # 注入 MCP 工具（main Agent）
        for tool_cls in get_mcp_tools_for_agent(self.plugin, "main"):
            registry.register(tool_cls)

        tool_schemas = registry.get_all()
        if tool_schemas:
            request.add_payload(LLMPayload(ROLE.TOOL, tool_schemas))  # type: ignore[arg-type]

        return request, registry

    def _build_system_prompt(self) -> str:
        """构建填充了人格信息的 system prompt。"""
        try:
            from src.core.config import get_core_config
            config = get_core_config()
            personality = config.personality
        except (RuntimeError, ImportError):
            personality = None

        def optional(val: str | None) -> str:
            return val if isinstance(val, str) and val.strip() else ""

        nickname = optional(getattr(personality, "nickname", "")) if personality else ""
        alias_names = "、".join(
            getattr(personality, "alias_names", []) or []
        ) if personality else ""
        personality_core = optional(getattr(personality, "personality_core", "")) if personality else ""
        personality_side = optional(getattr(personality, "personality_side", "")) if personality else ""
        identity = optional(getattr(personality, "identity", "")) if personality else ""
        reply_style = optional(getattr(personality, "reply_style", "")) if personality else ""
        background_story = optional(getattr(personality, "background_story", "")) if personality else ""
        terminal_environment = get_preferred_terminal_from_config(
            cast(CodingAgentConfig | None, getattr(self.plugin, "config", None))
        )

        prompt = render_prompt(MAIN_AGENT_SYSTEM_PROMPT,
            terminal_environment=terminal_environment,
            nickname=nickname or "Coding Agent",
            alias_names=alias_names or nickname or "Coding Agent",
            personality_core=personality_core or "是一个专业的编程助手",
            personality_side=personality_side or "",
            identity=identity or "编程智能体",
            reply_style=reply_style or "保持专业、简洁、清晰",
            background_story=background_story or "",
        )

        # 注入 Coder 模型 Profile 列表
        coder_profiles_text = self._get_coder_profiles_text()
        prompt = prompt.replace("[[coder_model_profiles]]", coder_profiles_text)

        return prompt

    def _get_coder_profiles_text(self) -> str:
        """从插件配置读取 model_profiles 并生成 prompt 文本。

        无 profiles 时返回空字符串（占位符被移除）。
        """
        config = getattr(self.plugin, "config", None)
        if config is None:
            return ""

        profiles: list = config.model_profiles
        if not profiles:
            return ""

        from .services.model_router import ModelRouter
        router = ModelRouter(profiles)
        return router.describe_for_prompt()

    async def _handle_tool_calls(
        self, response: Any, registry: ToolRegistry,
        trigger_msgs: list, session_id: str,
    ) -> Any:
        """处理工具调用循环，直到 LLM 不再调用工具。返回最终响应。

        与普通交互循环不同，这里会在每轮工具执行后主动拉取未读消息，
        将工作中追加的引导消息注入到下一轮请求，避免用户补充信息要等到整轮结束。
        """
        trigger_msg = trigger_msgs[-1] if trigger_msgs else None
        silent_tool_rounds = 0
        round_num = 0

        while True:
            calls = response.call_list
            if not calls:
                break

            round_num += 1
            logger.debug(f"工具调用轮次 {round_num}: {len(calls)} 个调用")

            # 通知前端即将执行的工具调用
            session_mgr = get_session_manager()
            for call in calls:
                # goal 审查轮：检测修改性工具
                if self._is_goal_review_round and call.name in (
                    "write", "edit", "create_plan", "implement_plan",
                ):
                    self._goal_round_modified = True
                await session_mgr.broadcast_to_session(session_id, {
                    "type": "tool.call",
                    "payload": {
                        "name": call.name,
                        "args_summary": self._summarize_args(
                            call.args if hasattr(call, "args") else {}
                        ),
                        "stage": "running",
                    },
                })

            # 启动 guidance 转发后台任务（供 implement_plan 内部的 CoderAgent 消费）
            forward_task = asyncio.create_task(
                self._forward_guidance_to_coder(session_id)
            )
            try:
                # 执行工具，结果会写入 response 的 TOOL_RESULT payload
                await self.run_tool_call(calls, response, registry, trigger_msg)
            finally:
                forward_task.cancel()
                try:
                    await forward_task
                except asyncio.CancelledError:
                    pass

            # 工具轮次之间主动接入工作中追加的用户消息，避免“引导无效直到回合结束”。
            follow_up_text, follow_up_unreads = await self.fetch_unreads()
            if follow_up_unreads:
                await self.flush_unreads(follow_up_unreads)
                response.add_payload(LLMPayload(ROLE.USER, Text(follow_up_text)))
                trigger_msg = follow_up_unreads[-1]
                await self._notify_status(
                    "coding",
                    "已接收新的引导消息，正在调整后续工具步骤...",
                )

            # 从 response 继续发送（包含工具结果）
            await self._notify_status("thinking", "根据工具结果继续分析...")
            try:
                response = await response.send(stream=True)
            except Exception as e:
                logger.error(f"LLM 流式请求失败 (round={round_num}): {e}")
                # 保留已执行的工具结果，注入错误信息后退出工具循环
                response.add_payload(LLMPayload(
                    ROLE.TOOL_RESULT,
                    ToolResult(
                        value=f"LLM 请求失败: {e}。已执行的工具结果已保留，"
                              f"请根据这些结果继续分析或重新尝试。",
                        call_id="stream_error",
                    ),
                ))
                break

            # 流式推送
            chunk_count = 0
            last_reasoning = ""
            try:
                async for event in response.stream_events():
                    chunk = event.text_delta or ""
                    if chunk:
                        chunk_count += 1
                        await self._stream_to_frontend(session_id, chunk)

                    reasoning = response.reasoning_content or ""
                    if reasoning and reasoning != last_reasoning:
                        last_reasoning = reasoning
                        await self._stream_thinking_to_frontend(session_id, reasoning)
            except Exception as e:
                logger.error(f"LLM 流式响应中断 (round={round_num}): {e}")
                # 流中断但 response 可能已有部分内容，保留并退出
                if not response.message:
                    response.add_payload(LLMPayload(
                        ROLE.TOOL_RESULT,
                        ToolResult(
                            value=f"流式响应中断: {e}。已执行的工具结果已保留，"
                                  f"请根据这些结果继续。",
                            call_id="stream_interrupt",
                        ),
                    ))
                break

            # 发送后续轮次的 thinking 内容（如有）
            if response.reasoning_content:
                await self._stream_thinking_to_frontend(
                    session_id,
                    response.reasoning_content,
                )

            logger.debug(
                f"工具后续响应: round={round_num}, chunks={chunk_count}, "
                f"tool_calls={len(response.call_list) if response.call_list else 0}"
            )

            silent_tool_rounds = advance_silent_tool_rounds(
                silent_tool_rounds,
                chunk_count=chunk_count,
                reasoning_content=response.reasoning_content,
                next_tool_call_count=len(response.call_list) if response.call_list else 0,
            )
            if response.call_list and silent_tool_rounds > 0:
                await self._notify_status(
                    "coding",
                    f"工具第 {round_num} 轮完成，但模型未返回可见文本，正在继续执行后续工具...",
                )
                if silent_tool_rounds >= self._MAX_SILENT_TOOL_ROUNDS:
                    raise RuntimeError(
                        "模型连续多轮仅请求工具且未返回可见文本/思考，已中止当前回合"
                    )

            if not response.call_list:
                break

        return response

    async def _forward_guidance_to_coder(self, session_id: str) -> None:
        """后台任务：将工作中追加的引导消息转发到 Coder Agent 的 guidance 队列。

        在 _handle_tool_calls 调用 implement_plan 等耗时工具期间运行，
        持续轮询 stream 的 unread 队列，将 guidance 消息推入 session.coder_guidance_queue，
        供 CoderAgent.execute() 在工具轮次间消费。

        依赖外部 cancellation 退出，每次循环间隔 0.3s。
        """
        session_mgr = get_session_manager()
        try:
            while True:
                follow_up_text, follow_up_unreads = await self.fetch_unreads()
                if follow_up_unreads:
                    session = session_mgr.get_session(session_id)
                    if session is not None:
                        session.coder_guidance_queue.append(follow_up_text)
                    await self.flush_unreads(follow_up_unreads)
                await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            # 外部取消，正常退出
            pass

    async def _push_context_usage(self, session_id: str, current: Any) -> None:
        """推送 LLM 上下文用量到前端。

        从 current._usage 提取 total_tokens，从 current.model_set 提取 max_context。
        如果 _usage 为 None（某些模型不返回），则不发送消息。
        """
        usage = getattr(current, "_usage", None)
        if not usage:
            return

        total_tokens = usage.get("total_tokens", 0)
        if not total_tokens:
            return

        # 从 model_set 获取 max_context（上下文窗口大小）
        max_context = 0
        model_set = getattr(current, "model_set", None)
        if model_set and isinstance(model_set, list) and len(model_set) > 0:
            max_context = model_set[0].get("max_context", 0) if isinstance(model_set[0], dict) else 0

        await get_session_manager().broadcast_to_session(session_id, {
            "type": "agent.context_usage",
            "payload": {
                "total_tokens": total_tokens,
                "max_context": max_context,
                "source": "agent",
            },
        })

    async def _notify_status(self, phase: str, detail: str) -> None:
        """推送 agent.status 消息到前端。"""
        session = get_session_manager().get_session_by_stream_id(self.stream_id)
        if session:
            await get_session_manager().broadcast_to_session(session.id, {
                "type": "agent.status",
                "payload": {"phase": phase, "detail": detail, "source": "agent"},
            })

    @staticmethod
    def _build_goal_review_prompt(goal_text: str) -> str:
        """构建目标审查 prompt。"""
        return (
            "<system_reminder>\n"
            "用户已经离线，你正在目标模式下工作。目标：" + goal_text + "\n\n"
            "请完全不要依赖任何前文上下文和历史对话，"
            "以全新的视角重新客观、严格地审视用户目标与当前实现的差距。"
            "找出任何尚未满足目标要求之处，或可进一步优化的地方，并立即修改完善。\n\n"
            "如果经过彻底审查后确实没有任何需要改动之处，"
            "请仅输出 GOAL COMPLETE，不要输出任何其他内容。\n\n"
            "重要规则：如果本轮你修改了任何文件、执行了任何代码变更，"
            "则绝对不能输出 GOAL COMPLETE。必须先进入下一轮审查，"
            "\n"
            "警告：退出 goal 模式必须十分谨慎，这会导致迭代循环停止，可能导致交付质量不够。"
            "因此在当你想要退出 goal 模式时，请务必首先思考有没有任何你没考虑到的可能性，"
            "以及现在的实现是否已经无可挑剔了。\n"
            "此消息将反复出现直到目标完成或者用户发送新的消息。"
            "</system_reminder>"
        )

    async def _stream_to_frontend(self, session_id: str, chunk: str) -> None:
        """推送 agent.text 流式 chunk 到前端。"""
        await get_session_manager().broadcast_to_session(session_id, {
            "type": "agent.text",
            "payload": {"content": chunk, "is_final": False},
        })

    async def _stream_thinking_to_frontend(
        self,
        session_id: str,
        content: str,
    ) -> None:
        """推送 agent.thinking 快照到前端。"""
        await get_session_manager().broadcast_to_session(session_id, {
            "type": "agent.thinking",
            "payload": {"content": content},
        })

    @staticmethod
    def _summarize_args(args: dict[str, Any] | str) -> str:
        """生成工具参数的简短摘要，用于前端显示。"""
        if not args:
            return ""
        if isinstance(args, str):
            summary = args.strip()
            if len(summary) > 120:
                summary = summary[:117] + "..."
            return summary
        parts = []
        for key, val in args.items():
            val_str = str(val)
            if len(val_str) > 60:
                val_str = val_str[:57] + "..."
            parts.append(f"{key}={val_str!r}")
        summary = ", ".join(parts)
        if len(summary) > 120:
            summary = summary[:117] + "..."
        return summary
