"""Coding Agent WebSocket 适配器。

使用标准 Adapter 模式接入框架消息管线：
  TUI (WebSocket) → from_platform_message → CoreSink → 分发器 → Chatter
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from mofox_wire import (
    CoreSink,
    MessageBuilder,
    MessageEnvelope,
    WebSocketAdapterOptions,
)
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.asyncio.server import serve as ws_serve

from src.app.plugin_system.base import BaseAdapter, BasePlugin
from src.app.plugin_system.api import service_api
from src.kernel.logger import get_logger

from .session_manager import get_session_manager

logger = get_logger("coding_agent.adapter")


class CodingAgentAdapter(BaseAdapter):
    """Coding Agent 适配器 — WebSocket 服务器模式。

    管理多个并发 TUI 连接，每个连接对应一个 CodingSession。
    用户消息通过标准管线（CoreSink → distributor → stream loop → Chatter）处理，
    Chatter 响应仍通过 SessionManager 直接推送至 WebSocket。
    """

    adapter_name = "coding_agent_adapter"
    adapter_version = "0.1.0"
    adapter_author = "MoFox Team"
    adapter_description = "Coding Agent TUI WebSocket 适配器"
    platform = "coding_agent"

    run_in_subprocess = False

    def __init__(
        self,
        core_sink: CoreSink,
        plugin: BasePlugin | None = None,
        **kwargs: Any,
    ) -> None:
        host = "0.0.0.0"
        port = 8765
        path = "/coding-agent/ws"
        tui_username = "TUI User"

        if plugin and hasattr(plugin, "config") and plugin.config:
            cfg = plugin.config
            ws_cfg = getattr(cfg, "ws", None)
            if ws_cfg:
                host = getattr(ws_cfg, "host", host)
                port = getattr(ws_cfg, "port", port)
                path = getattr(ws_cfg, "path", path)
                tui_username = getattr(ws_cfg, "tui_username", tui_username)

        transport = WebSocketAdapterOptions(
            mode="server",
            url=f"ws://{host}:{port}{path}",
        )

        super().__init__(core_sink, plugin=plugin, transport=transport, **kwargs)

        self._tui_username = tui_username

        # conn_id → WebSocketLike
        self._connections: dict[str, Any] = {}
        # conn_id → session_id
        self._conn_sessions: dict[str, str] = {}
        # session_id → WebSocketLike (for SessionManager outbound)
        self._session_ws: dict[str, Any] = {}

    # ── 生命周期 ──────────────────────────────────────────

    async def on_adapter_loaded(self) -> None:
        logger.info("Coding Agent 适配器已加载")

    async def on_adapter_unloaded(self) -> None:
        for ws in list(self._session_ws.values()):
            try:
                await ws.close()
            except Exception:
                pass
        self._connections.clear()
        self._conn_sessions.clear()
        self._session_ws.clear()
        logger.info("Coding Agent 适配器已卸载")

    # ── 健康检查重写 ─────────────────────────────────────

    def is_connected(self) -> bool:
        """服务器模式：只要 WebSocket server 还在监听就视为已连接。"""
        return self._ws_server is not None

    async def health_check(self) -> bool:
        return self.is_connected()

    async def reconnect(self) -> None:
        """服务器模式不需要自动重连，服务器始终监听。"""
        logger.debug("Coding Agent 服务器模式跳过自动重连")

    # ── 多连接 WebSocket 服务器 ──────────────────────────

    async def _start_ws_server(self, options: WebSocketAdapterOptions) -> None:
        """重写：支持多个并发 TUI 连接。"""
        parsed = urlparse(options.url)
        host = parsed.hostname or "0.0.0.0"
        port = parsed.port or 8765
        path = parsed.path or "/coding-agent/ws"

        async def handler(ws: Any) -> None:
            # 新版 websockets.asyncio.server: path 通过 ws.request.path 访问
            ws_path = getattr(getattr(ws, "request", None), "path", None) or getattr(ws, "path", "/")
            if ws_path != path:
                await ws.close(code=4000, reason="Path mismatch")
                return
            await self._handle_connection(ws)

        self._ws_server = await ws_serve(
            handler,
            host,
            port,
            ping_interval=None,       # 禁用服务端 keepalive ping，客户端负责
            ping_timeout=None,
            close_timeout=5,          # 加速关闭，减少 _drain_helper 竞态窗口
            max_size=options.max_message_size,
        )
        logger.info(f"Coding Agent WS 服务器启动: ws://{host}:{port}{path}")

    async def _handle_connection(self, ws: Any) -> None:
        """处理单个 TUI WebSocket 连接的完整生命周期。"""
        conn_id = str(uuid4())
        self._connections[conn_id] = ws
        try:
            async for raw in ws:
                try:
                    payload = self._parse_incoming(raw)
                    await self._handle_raw_with_conn(payload, conn_id)
                except Exception:
                    logger.error("处理 TUI 消息异常")
        except ConnectionClosedOK:
            logger.info(f"TUI 连接 {conn_id[:8]} 正常关闭")
        except ConnectionClosedError as e:
            logger.warning(f"TUI 连接 {conn_id[:8]} 异常关闭 (code={e.code})")
        except Exception:
            logger.error(f"TUI 连接 {conn_id[:8]} 异常断开")
        finally:
            self._connections.pop(conn_id, None)
            session_id = self._conn_sessions.pop(conn_id, None)
            if session_id:
                # 在 destroy 前断开 websocket 引用，防止 broadcast_to_session 尝试向已关闭的连接发送
                self._session_ws.pop(session_id, None)
                session_mgr = get_session_manager()
                session = session_mgr.get_session(session_id)
                if session:
                    session.websocket = None
                await session_mgr.destroy_session(session_id)
            logger.info(f"TUI 连接 {conn_id[:8]} 已关闭")

    async def _handle_raw_with_conn(self, raw: dict, conn_id: str) -> None:
        """处理来自特定连接的消息。"""
        msg_type = raw.get("type", "")
        session_mgr = get_session_manager()

        # ── 会话初始化 ──
        if msg_type == "session.init":
            payload = raw.get("payload", {})
            working_directory = payload.get("working_directory", ".")
            session_id = payload.get("session_id", "")

            # 清理旧会话（/new 或重新 init 时避免孤儿会话）
            old_session_id = self._conn_sessions.get(conn_id)
            if old_session_id and old_session_id != session_id:
                await session_mgr.destroy_session(old_session_id)
                self._session_ws.pop(old_session_id, None)

            if session_id:
                # 恢复模式
                session, resume_warning = await session_mgr.resume_session(
                    conn_id=conn_id,
                    working_directory=working_directory,
                    session_id=session_id,
                )
                if session is None:
                    # 恢复失败，回退到新建
                    session = await session_mgr.create_session(
                        conn_id=conn_id,
                        working_directory=working_directory,
                    )
                    resume_warning = ""
            else:
                # 新建模式
                session = await session_mgr.create_session(
                    conn_id=conn_id,
                    working_directory=working_directory,
                )
                resume_warning = ""

            self._conn_sessions[conn_id] = session.id
            self._session_ws[session.id] = self._connections[conn_id]
            session.websocket = self._connections[conn_id]

            # 构建 session.ready 事件
            ready_payload = {
                "session_id": session.id,
                "project_name": (
                    working_directory.rstrip("/\\").split("/")[-1]
                    or working_directory.rstrip("/\\").split("\\")[-1]
                ),
                "title": session._title or "",
            }

            # 如果是恢复模式，添加历史消息和目录不匹配警告
            if session_id and session._resume_payloads is not None:
                history = []
                for pdata in session._resume_payloads:
                    role = pdata.get("role", "")
                    content_items = pdata.get("content", [])
                    
                    # 提取文本内容
                    text_parts = []
                    for item in content_items:
                        if item.get("__type__") == "Text":
                            text_parts.append(item.get("text", ""))
                    
                    if text_parts:
                        text_content = "\n".join(text_parts)
                        history.append({
                            "role": role,
                            "content": text_content,
                        })
                
                ready_payload["history"] = history

            # 如果有 working_directory 不匹配警告，加入 payload
            if resume_warning:
                ready_payload["working_directory_mismatch"] = resume_warning

            await session_mgr.broadcast_to_session(session.id, {
                "type": "session.ready",
                "payload": ready_payload,
            })
            return

        # ── 需要会话上下文的消息 ──
        session_id = self._conn_sessions.get(conn_id)
        if not session_id:
            return

        session = session_mgr.get_session(session_id)
        if not session:
            return

        if msg_type == "user.message":
            # 转为 MessageEnvelope 送入标准管线
            envelope = await self._build_user_envelope(raw, session_id=session.id)
            if envelope:
                # 预计算 stream_id 并绑定到 session（与 distributor 使用相同逻辑）
                from src.core.transport.message_receive.utils import extract_stream_id
                stream_id = extract_stream_id(envelope["message_info"])
                session_mgr.bind_stream_id(session_id=session.id, stream_id=stream_id)
                await self.core_sink.send(envelope)

        elif msg_type == "session.list":
            # 列出历史会话
            working_directory = session.working_directory
            summaries = await session_mgr.list_sessions(working_directory)
            await session_mgr.broadcast_to_session(session_id, {
                "type": "session.list_result",
                "payload": {
                    "sessions": [
                        {
                            "session_id": s.session_id,
                            "title": s.title,
                            "created_at": s.created_at,
                            "last_active_at": s.last_active_at,
                            "message_count": s.message_count,
                            "phase": s.phase,
                        }
                        for s in summaries
                    ],
                },
            })

        elif msg_type == "session.delete":
            payload = raw.get("payload", {})
            target_id = payload.get("session_id", "")
            if target_id:
                await session_mgr.delete_session(session.working_directory, target_id)
                await session_mgr.broadcast_to_session(session_id, {
                    "type": "session.delete_result",
                    "payload": {"session_id": target_id, "success": True},
                })

        elif msg_type == "bash.approval":
            payload = raw.get("payload", {})
            await session_mgr.submit_approval(
                session_id,
                payload.get("request_id", ""),
                payload.get("decision", "deny"),
                payload.get("prefix", ""),
                payload.get("reason", ""),
            )

        elif msg_type == "user.interrupt":
            await session_mgr.broadcast_to_session(session_id, {
                "type": "agent.status",
                "payload": {"phase": "ready", "detail": "操作已中断", "source": "agent"},
            })

        elif msg_type == "auto_review.toggle":
            session.auto_review_enabled = raw.get("payload", {}).get("enabled", False)

        elif msg_type == "yolo.toggle":
            session.yolo_mode = raw.get("payload", {}).get("enabled", False)

        elif msg_type == "goal.set":
            session.goal_mode = True
            session.goal_text = raw.get("payload", {}).get("text", "")
            # 将目标作为 user message 注入管线
            content = (
                f"你已进入目标模式，用户已经离线，你需要自主完成以下目标：\n\n"
                f"{session.goal_text}\n\n"
                f"请立即开始分析目标并制定实施计划。"
            )
            envelope = await self._build_user_envelope(
                {"payload": {"content": content, "kind": "message"}},
                session_id=session.id,
            )
            if envelope:
                from src.core.transport.message_receive.utils import extract_stream_id
                stream_id = extract_stream_id(envelope["message_info"])
                session_mgr.bind_stream_id(session_id=session.id, stream_id=stream_id)
                await self.core_sink.send(envelope)

        elif msg_type == "checkpoint.rollback":
            await self._handle_rollback(session, raw.get("payload", {}))

        elif msg_type == "checkpoint.list":
            await self._handle_checkpoint_list(session)

        elif msg_type == "session.link":
            await self._handle_link(session, raw.get("payload", {}))

        elif msg_type == "session.close":
            # 前端主动关闭会话：清理连接映射和内存会话，保留磁盘数据
            self._conn_sessions.pop(conn_id, None)
            self._session_ws.pop(session_id, None)
            session.websocket = None
            await session_mgr.destroy_session(session_id)
            logger.debug(f"会话 {session_id[:8]} 已由前端主动关闭")

    # ── 消息转换 ─────────────────────────────────────────

    async def from_platform_message(self, raw: Any) -> MessageEnvelope | None:
        """不会被直接调用（消息在 _handle_connection 中处理）。"""
        return None

    async def _build_user_envelope(self, raw: dict, session_id: str) -> MessageEnvelope | None:
        """将 user.message 转为标准 MessageEnvelope。

        使用 session_id 作为 user_id，确保每个 session 拥有独立的聊天流，
        避免多 session 之间状态污染。
        """
        payload = raw.get("payload", {})
        content = payload.get("content", "")
        if not content:
            return None

        kind = str(payload.get("kind", "message") or "message").strip().lower()
        if kind == "guidance":
            content = f"【工作中追加引导】\n{content}"

        msg_id = str(uuid4())
        builder = MessageBuilder()
        builder.direction("incoming")
        builder.message_id(msg_id)
        builder.timestamp_ms(int(time.time() * 1000))
        builder.from_user(
            user_id=session_id,
            platform="coding_agent",
            nickname=self._tui_username,
        )
        builder.text(content)
        builder.format_info(
            content_format=["text"],
            accept_format=["text", "markdown"],
        )

        envelope = builder.build()
        envelope["raw_message"] = raw
        return envelope

    # ── 出站（Chatter 通过 SessionManager 直接推送，此处为占位） ──

    async def _send_platform_message(self, envelope: MessageEnvelope) -> None:
        """占位实现 — Chatter 通过 SessionManager.broadcast_to_session 直接发送。"""
        pass

    # ── 辅助方法 ─────────────────────────────────────────

    @staticmethod
    def _parse_incoming(raw: str | bytes) -> dict:
        """解析 WebSocket 文本帧为 dict。"""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    async def _handle_rollback(self, session: Any, payload: dict) -> None:
        if not session.checkpoint_manager:
            return
        mode = payload.get("mode", "last")
        if mode == "last":
            result = await session.checkpoint_manager.rollback_last()
        else:
            checkpoint_id = payload.get("checkpoint_id", "")
            result = await session.checkpoint_manager.rollback_to(checkpoint_id)
        session_mgr = get_session_manager()
        await session_mgr.broadcast_to_session(session.id, {
            "type": "checkpoint.rollback_result",
            "payload": {
                "rolled_back": result.rolled_back_checkpoints,
                "restored_files": result.restored_files,
                "warnings": result.warnings,
            },
        })

    async def _handle_checkpoint_list(self, session: Any) -> None:
        if not session.checkpoint_manager:
            return
        checkpoints = session.checkpoint_manager.list_checkpoints()
        session_mgr = get_session_manager()
        await session_mgr.broadcast_to_session(session.id, {
            "type": "checkpoint.list_result",
            "payload": {"checkpoints": checkpoints},
        })

    async def _handle_link(self, session: Any, payload: dict) -> None:
        """处理 session.link 消息，关联外部项目目录。"""
        from pathlib import Path
        
        session_mgr = get_session_manager()
        
        # 1. 解析规范化路径
        raw_path = payload.get("path", "")
        if not raw_path:
            await session_mgr.broadcast_to_session(session.id, {
                "type": "link.result",
                "payload": {
                    "path": "",
                    "status": "error",
                    "message": "未提供路径",
                },
            })
            return
        
        # 规范化路径
        try:
            resolved_path = Path(raw_path).resolve()
            normalized_path = str(resolved_path)
        except Exception as e:
            await session_mgr.broadcast_to_session(session.id, {
                "type": "link.result",
                "payload": {
                    "path": raw_path,
                    "status": "error",
                    "message": f"路径解析失败: {e}",
                },
            })
            return
        
        # 2. 校验路径
        # 检查路径是否存在
        if not resolved_path.exists():
            await session_mgr.broadcast_to_session(session.id, {
                "type": "link.result",
                "payload": {
                    "path": normalized_path,
                    "status": "error",
                    "message": f"路径不存在: {normalized_path}",
                },
            })
            return
        
        # 检查是否为目录
        if not resolved_path.is_dir():
            await session_mgr.broadcast_to_session(session.id, {
                "type": "link.result",
                "payload": {
                    "path": normalized_path,
                    "status": "error",
                    "message": f"路径不是目录: {normalized_path}",
                },
            })
            return
        
        # 检查是否已关联
        if normalized_path in session.linked_directories:
            await session_mgr.broadcast_to_session(session.id, {
                "type": "link.result",
                "payload": {
                    "path": normalized_path,
                    "status": "already_linked",
                    "message": f"目录已关联: {normalized_path}",
                },
            })
            return
        
        # 检查是否为主工作目录或其子目录（已在范围内，无需 link）
        work_dir = Path(session.working_directory).resolve()
        try:
            resolved_path.relative_to(work_dir)
            await session_mgr.broadcast_to_session(session.id, {
                "type": "link.result",
                "payload": {
                    "path": normalized_path,
                    "status": "already_linked",
                    "message": f"目录已在主工作目录范围内，无需关联: {normalized_path}",
                },
            })
            return
        except ValueError:
            pass  # 不在主工作目录内，继续

        # 检查是否在某个已关联目录的子目录内
        for linked in session.linked_directories:
            try:
                resolved_path.relative_to(Path(linked).resolve())
                await session_mgr.broadcast_to_session(session.id, {
                    "type": "link.result",
                    "payload": {
                        "path": normalized_path,
                        "status": "already_linked",
                        "message": f"目录已在关联目录 {linked} 范围内，无需重复关联",
                    },
                })
                return
            except ValueError:
                pass

        # 检查是否已有更深的子目录被 link（避免路径覆盖歧义）
        for linked in session.linked_directories:
            linked_path = Path(linked).resolve()
            try:
                linked_path.relative_to(resolved_path)
                await session_mgr.broadcast_to_session(session.id, {
                    "type": "link.result",
                    "payload": {
                        "path": normalized_path,
                        "status": "error",
                        "message": f"已关联子目录 {linked}，请先取消该关联后再 link 父目录",
                    },
                })
                return
            except ValueError:
                pass
        
        # 3. 确保 stream_id 已绑定（新会话和恢复会话此时 session.stream_id 为空）
        from src.core.models.stream import ChatStream

        if not session.stream_id:
            stream_id = ChatStream.generate_stream_id(
                platform="coding_agent",
                user_id=session.id,
            )
            session_mgr.bind_stream_id(session_id=session.id, stream_id=stream_id)

        # 4. 读取或生成项目上下文
        project_context_service = service_api.get_service("coding_agent:service:project_context")
        context = await project_context_service.load_context(normalized_path)
        
        research_triggered = False
        if context is None:
            # 没有缓存，运行完整研究（scout + 并行模块研究 + 虚拟环境检测）
            from .orchestration import CodingOrchestrator
            orchestrator = CodingOrchestrator(self.plugin, session.stream_id)
            
            # 广播研究进度的回调
            async def _broadcast_link_research_progress(payload: dict[str, Any]) -> None:
                await session_mgr.broadcast_to_session(session.id, {
                    "type": "research.progress",
                    "payload": payload,
                })
            
            context = await orchestrator.run_full_research(
                normalized_path,
                progress_callback=_broadcast_link_research_progress,
            )
            
            # 保存到缓存
            await project_context_service.save_context(normalized_path, context)
            research_triggered = True
            
        elif "virtual_environment" not in context:
            # 有缓存但缺少虚拟环境字段，补充检测
            venv_info = await project_context_service.detect_project_virtual_env(normalized_path)
            context["virtual_environment"] = venv_info
            
            # 更新缓存
            await project_context_service.save_context(normalized_path, context)
        
        # 5. 添加到关联目录列表
        session.linked_directories.append(normalized_path)
        
        # 6. 注册/更新 linked_projects system reminder
        from src.app.plugin_system.api.prompt_api import add_system_reminder
        
        # 为所有已关联项目构建结构化描述
        linked_blocks: list[str] = []
        for linked_dir in session.linked_directories:
            linked_ctx = await project_context_service.load_context(linked_dir)
            block = _build_linked_project_block(linked_dir, linked_ctx)
            linked_blocks.append(block)
        
        reminder_content = "<linked_projects>\n" + "\n".join(linked_blocks) + "\n</linked_projects>"
        
        add_system_reminder(
            bucket="code_main_agent",
            name="linked_projects",
            content=reminder_content,
            insert_type="dynamic",
            consume="forever",
        )
        # 同时注入 Coder 的独立槽位，Coder 通过 with_reminder="code_coder" 自动获取
        add_system_reminder(
            bucket="code_coder",
            name="linked_projects",
            content=reminder_content,
            insert_type="dynamic",
            consume="forever",
        )
        
        # 7. 构建 user envelope 并注入对话
        from mofox_wire import MessageBuilder
        import time
        from uuid import uuid4
        
        project_name = Path(normalized_path).name
        venv_info = context.get("virtual_environment", "未检测到")
        
        user_content = _build_link_user_message(normalized_path, context)
        
        envelope = MessageBuilder() \
            .direction("incoming") \
            .message_id(str(uuid4())) \
            .timestamp_ms(int(time.time() * 1000)) \
            .from_user(
                user_id=session.id,
                platform="coding_agent",
                nickname="TUI User",
            ) \
            .text(user_content) \
            .build()
        
        # stream_id 已在第 3 步绑定，直接发送
        await self.core_sink.send(envelope)

        # 8. 广播 link.result 到前端
        await session_mgr.broadcast_to_session(session.id, {
            "type": "link.result",
            "payload": {
                "path": normalized_path,
                "status": "ok",
                "project_name": project_name,
                "virtual_environment": venv_info,
                "research_triggered": research_triggered,
                "message": f"项目已链接: {project_name}",
            },
        })

    async def get_bot_info(self) -> dict[str, Any]:
        return {
            "bot_id": "coding_agent",
            "bot_name": "Coding Agent",
            "platform": self.platform,
        }


# ── 模块级辅助函数（供 _handle_link 使用）─────────────────


def _build_linked_project_block(linked_dir: str, context: dict | None) -> str:
    """从项目上下文构建 <linked_project> 结构化描述块。

    返回包含 scout 摘要和模块研究详情的 XML 片段，注入到
    linked_projects system_reminder 中供 LLM 理解关联项目。
    """
    from pathlib import Path

    project_name = Path(linked_dir).name

    if not context:
        return f'<linked_project name="{project_name}" path="{linked_dir}">\n  (上下文未加载)\n</linked_project>'

    scout = context.get("scout", {}) if isinstance(context.get("scout"), dict) else {}
    venv_info = context.get("virtual_environment") or scout.get("virtual_environment", "未检测到")
    modules_list = context.get("modules", [])

    lines: list[str] = []
    lines.append(f'<linked_project name="{project_name}" path="{linked_dir}">')

    # 基本信息
    lines.append(f"  项目名称: {project_name}")
    lines.append(f"  路径: {linked_dir}")
    if venv_info and venv_info != "未检测到":
        lines.append(f"  虚拟环境: {venv_info}")

    # scout 摘要
    tech_stack = scout.get("tech_stack", [])
    if tech_stack:
        lines.append(f"  技术栈: {', '.join(tech_stack[:10])}")

    source_root = scout.get("source_root", "")
    if source_root:
        lines.append(f"  源代码根目录: {source_root}")

    build_system = scout.get("build_system", "")
    if build_system:
        lines.append(f"  构建系统: {build_system}")

    scout_modules = scout.get("modules", [])
    if scout_modules:
        lines.append(f"  模块数量: {len(scout_modules)}")

    scout_summary = scout.get("summary", "")
    if scout_summary:
        # 截断过长的摘要
        if len(scout_summary) > 600:
            scout_summary = scout_summary[:600] + "..."
        lines.append(f"  项目摘要: {scout_summary}")

    # 模块研究详情
    if modules_list:
        lines.append("  模块详情:")
        for mod in modules_list:
            mod_path = mod.get("path", "?")
            mod_report = mod.get("report", {})
            if not isinstance(mod_report, dict):
                lines.append(f"    [{mod_path}] 研究报告不可用")
                continue

            mod_purpose = mod_report.get("purpose", "")
            mod_summary = mod_report.get("summary", "")

            lines.append(f"    [{mod_path}]")
            if mod_purpose:
                lines.append(f"      用途: {mod_purpose}")
            if mod_summary:
                # 截断过长的摘要
                if len(mod_summary) > 400:
                    mod_summary = mod_summary[:400] + "..."
                lines.append(f"      摘要: {mod_summary}")

            # 关键类
            key_classes = mod_report.get("key_classes", [])
            if key_classes:
                lines.append(f"      关键类 ({len(key_classes)}):")
                for kc in key_classes[:5]:  # 最多5个
                    kc_name = kc.get("name", "?")
                    kc_desc = kc.get("description", "")
                    if kc_desc and len(kc_desc) > 120:
                        kc_desc = kc_desc[:120] + "..."
                    lines.append(f"        • {kc_name}: {kc_desc}")

            # 关键函数
            key_functions = mod_report.get("key_functions", [])
            if key_functions:
                lines.append(f"      关键函数 ({len(key_functions)}):")
                for kf in key_functions[:5]:
                    kf_name = kf.get("name", "?")
                    kf_desc = kf.get("description", "")
                    if kf_desc and len(kf_desc) > 120:
                        kf_desc = kf_desc[:120] + "..."
                    lines.append(f"        • {kf_name}: {kf_desc}")

            # 公开 API
            public_api = mod_report.get("public_api", [])
            if public_api:
                api_names = [
                    item.get("name", "?") if isinstance(item, dict) else str(item)
                    for item in public_api[:5]
                ]
                lines.append(f"      公开 API: {', '.join(api_names)}")

    lines.append("</linked_project>")
    return "\n".join(lines)


def _build_link_user_message(linked_path: str, context: dict | None) -> str:
    """构建发送给 chatter 的 link 用户消息，包含项目结构概况。"""
    from pathlib import Path

    project_name = Path(linked_path).name

    if not context:
        return (
            f"我关联了一个外部项目目录: {linked_path}\n"
            f"项目名称: {project_name}\n"
            "(该项目尚未完成上下文研究)"
        )

    scout = context.get("scout", {}) if isinstance(context.get("scout"), dict) else {}
    venv_info = context.get("virtual_environment") or scout.get("virtual_environment", "未检测到")
    modules_list = context.get("modules", [])
    scout_modules = scout.get("modules", [])

    lines: list[str] = []
    lines.append(f"我关联了一个外部项目目录: {linked_path}")
    lines.append(f"项目名称: {project_name}")

    if venv_info and venv_info != "未检测到":
        lines.append(f"虚拟环境: {venv_info}")

    tech_stack = scout.get("tech_stack", [])
    if tech_stack:
        lines.append(f"技术栈: {', '.join(tech_stack[:10])}")

    source_root = scout.get("source_root", "")
    if source_root:
        lines.append(f"源代码根目录: {source_root}")

    build_system = scout.get("build_system", "")
    if build_system:
        lines.append(f"构建系统: {build_system}")

    scout_summary = scout.get("summary", "")
    if scout_summary:
        if len(scout_summary) > 500:
            scout_summary = scout_summary[:500] + "..."
        lines.append(f"项目摘要: {scout_summary}")

    config_files = scout.get("config_files", [])
    if config_files:
        lines.append(f"配置文件: {', '.join(config_files[:5])}")

    # 模块概况
    if scout_modules or modules_list:
        lines.append("")
        lines.append("=== 模块结构概况 ===")
        for mod in scout_modules:
            mod_path = mod.get("path", "?")
            mod_desc = mod.get("description", "")
            mod_files = mod.get("estimated_files", "?")
            lines.append(f"  [{mod_path}] {mod_desc} (预估 {mod_files} 个文件)")

        # 已研究的模块详情（key classes/functions）
        if modules_list:
            lines.append("")
            lines.append("=== 已研究的模块详情 ===")
            for mod in modules_list:
                mod_path = mod.get("path", "?")
                mod_success = mod.get("success", False)
                mod_report = mod.get("report", {})
                if not isinstance(mod_report, dict) or not mod_success:
                    lines.append(f"  [{mod_path}] 研究未完成或不可用")
                    continue

                mod_purpose = mod_report.get("purpose", "")
                lines.append(f"  [{mod_path}]")
                if mod_purpose:
                    lines.append(f"    用途: {mod_purpose}")

                key_classes = mod_report.get("key_classes", [])
                if key_classes:
                    lines.append(f"    关键类 ({len(key_classes)}):")
                    for kc in key_classes[:5]:
                        kc_name = kc.get("name", "?")
                        kc_file = kc.get("file", "")
                        kc_desc = kc.get("description", "")
                        if kc_desc and len(kc_desc) > 100:
                            kc_desc = kc_desc[:100] + "..."
                        location = f" ({kc_file})" if kc_file else ""
                        lines.append(f"      • {kc_name}{location}: {kc_desc}")

                key_functions = mod_report.get("key_functions", [])
                if key_functions:
                    lines.append(f"    关键函数 ({len(key_functions)}):")
                    for kf in key_functions[:5]:
                        kf_name = kf.get("name", "?")
                        kf_desc = kf.get("description", "")
                        if kf_desc and len(kf_desc) > 100:
                            kf_desc = kf_desc[:100] + "..."
                        lines.append(f"      • {kf_name}: {kf_desc}")

                public_api = mod_report.get("public_api", [])
                if public_api:
                    api_names = [
                        item.get("name", "?") if isinstance(item, dict) else str(item)
                        for item in public_api[:5]
                    ]
                    lines.append(f"    公开 API: {', '.join(api_names)}")

    return "\n".join(lines)
