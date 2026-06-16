"""Coding Agent 会话管理器。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable, Any, Callable
from uuid import uuid4

from src.kernel.logger import get_logger

from .checkpoint_manager import CheckpointManager
from .permission_manager import PermissionManager
from .session_store import (
    SessionData,
    SessionStore,
    SessionSummary,
    build_timeline_from_payloads,
)

logger = get_logger("coding_agent.session_manager")


@runtime_checkable
class WebSocketLike(Protocol):
    """适配器提供的 WebSocket 接口（兼容 websockets 库协议）。"""

    async def send(self, data: str | bytes) -> None: ...


@dataclass
class CodingSession:
    """一个前端连接对应的会话。"""
    id: str
    conn_id: str  # adapter 连接 ID
    working_directory: str
    websocket: WebSocketLike | None = None  # 由 adapter 设置
    stream_id: str = ""  # 关联的 ChatStream ID
    auto_review_enabled: bool = False
    yolo_mode: bool = False
    goal_mode: bool = False
    goal_text: str = ""
    goal_doc_path: str = ""
    checkpoint_manager: CheckpointManager | None = None
    permission_manager: PermissionManager | None = None
    pending_approvals: dict[str, asyncio.Event] = field(default_factory=dict)
    approval_results: dict[str, str] = field(default_factory=dict)
    approval_prefixes: dict[str, str] = field(default_factory=dict)
    approval_reasons: dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    phase: str = "init"
    # ── 会话持久化字段 ──
    session_store: SessionStore | None = None       # 持久化存储
    _resume_payloads: list[dict] | None = None      # 恢复用的序列化 payloads
    _resume_project_context: dict | None = None     # 恢复用的 project_context
    _title: str = ""                                # 标题（内存缓存）
    linked_directories: list[str] = field(default_factory=list)  # 关联的外部项目目录
    coder_guidance_queue: list[str] = field(default_factory=list)  # chatter→coder 工作中追加引导传递
    _coder_payloads_data: list[dict] | None = None  # Coder Agent 执行中的中间 payloads（供 _auto_save 保存）
    staging_area: Any = None  # FileStagingArea | None，Coder 执行期间的文件暂存区
    usage_total: dict[str, dict[str, Any]] = field(default_factory=dict)  # 按 model_name 累计用量
    solo_mode: bool = False  # Solo 模式：单一 agent 完成所有工作
    solo_model: str = ""  # Solo 模式使用的模型名（model.toml 中 models 的 name）
    main_model: str = ""  # 默认模式下覆盖主 Agent 使用的模型名（model.toml 中 models 的 name）
    payloads_data: list[dict[str, Any]] = field(default_factory=list)  # 当前会话的序列化 payload 快照
    timeline_events: list[dict[str, Any]] = field(default_factory=list)  # 可恢复的前端消息时间线
    conversation_markers: list[dict[str, Any]] = field(default_factory=list)  # 用户撤回 / fork 边界
    stream_buffers: dict[str, dict[str, Any]] = field(default_factory=dict)  # source -> 流式响应缓冲区
    thinking_buffers: dict[str, dict[str, Any]] = field(default_factory=dict)  # source -> thinking 快照缓冲区
    last_completed_agent_message_ids: dict[str, str] = field(default_factory=dict)  # source -> 最后封口的 agent 消息 ID
    interrupt_requested: bool = False
    active_runtime_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    active_processes: dict[int, Any] = field(default_factory=dict)


class SessionManager:
    """管理所有活跃的 Coding Agent 会话。"""

    def __init__(self) -> None:
        self._sessions: dict[str, CodingSession] = {}  # session_id -> session
        self._stream_to_session: dict[str, str] = {}  # stream_id -> session_id
        self._permission_managers: dict[str, PermissionManager] = {}  # work_dir -> manager
        self._store_cache: dict[str, SessionStore] = {}  # work_dir -> SessionStore

    def _get_store(self, working_directory: str) -> SessionStore:
        """获取或创建 working_directory 对应的 SessionStore（缓存）。"""
        if working_directory not in self._store_cache:
            self._store_cache[working_directory] = SessionStore(working_directory)
        return self._store_cache[working_directory]

    async def create_session(
        self, conn_id: str, working_directory: str
    ) -> CodingSession:
        """创建新会话。"""
        session_id = str(uuid4())

        checkpoint_mgr = CheckpointManager(session_id, working_directory)

        if working_directory not in self._permission_managers:
            self._permission_managers[working_directory] = PermissionManager(working_directory)
        perm_mgr = self._permission_managers[working_directory]

        session = CodingSession(
            id=session_id,
            conn_id=conn_id,
            working_directory=working_directory,
            checkpoint_manager=checkpoint_mgr,
            permission_manager=perm_mgr,
            session_store=self._get_store(working_directory),
        )

        self._sessions[session_id] = session
        return session

    async def resume_session(
        self, conn_id: str, working_directory: str, session_id: str
    ) -> tuple[CodingSession | None, str]:
        """从持久化存储恢复会话。

        不会检查 TTL 过期——恢复时信任缓存。

        Returns:
            (session, warning) 元组，warning 非空时表示 working_directory 不匹配。
        """
        store = self._get_store(working_directory)
        data = await store.load(session_id)
        if data is None:
            logger.warning(f"会话 {session_id} 不存在，无法恢复")
            return None, ""

        # 校验 working_directory 一致性
        warning = ""
        stored_dir = data.working_directory
        if stored_dir and stored_dir != working_directory:
            logger.warning(
                f"恢复会话 {session_id[:8]} 时 working_directory 不匹配: "
                f"客户端={working_directory}, 存储={stored_dir}，使用存储值"
            )
            warning = (
                f"工作目录不匹配：会话创建于 {stored_dir}，"
                f"当前为 {working_directory}。将使用原始目录恢复。"
            )
            # 使用存储的 working_directory 恢复，因为 session 数据和文件都在那个目录下
            working_directory = stored_dir

        if working_directory not in self._permission_managers:
            self._permission_managers[working_directory] = PermissionManager(working_directory)
        perm_mgr = self._permission_managers[working_directory]

        # 恢复 checkpoint：如果有持久化的 checkpoint 数据则重建 CheckpointManager
        checkpoint_mgr = CheckpointManager(session_id, working_directory)
        if data.checkpoints:
            checkpoint_mgr = CheckpointManager.from_checkpoint_dicts(
                session_id, working_directory, data.checkpoints,
            )

        session = CodingSession(
            id=session_id,
            conn_id=conn_id,
            working_directory=working_directory,
            checkpoint_manager=checkpoint_mgr,
            permission_manager=perm_mgr,
            session_store=store,
            _resume_payloads=data.payloads,
            _resume_project_context=data.project_context,
            _title=data.title,
            created_at=data.created_at,
            linked_directories=data.linked_directories,
            phase="ready",  # 恢复后直接进入 ready
            usage_total=dict(data.usage_total) if data.usage_total else {},
            solo_mode=data.solo_mode,
            solo_model=data.solo_model,
            main_model=data.main_model,
            auto_review_enabled=data.auto_review_enabled,
            yolo_mode=data.yolo_mode,
            payloads_data=deepcopy(data.payloads),
            timeline_events=deepcopy(data.timeline) if data.timeline else build_timeline_from_payloads(data.payloads),
            conversation_markers=deepcopy(data.conversation_markers),
        )

        self._sessions[session_id] = session
        logger.info(f"会话 {session_id[:8]} 已从磁盘恢复，标题: {data.title!r}")
        return session, warning

    async def save_session_state(
        self, session_id: str, payloads: list[dict],
        project_context: dict | None = None,
        title: str | None = None,
        message_count: int | None = None,
        linked_directories: list[str] | None = None,
        coder_payloads: list[dict] | None = None,
    ) -> None:
        """保存当前会话状态到磁盘。"""
        session = self._sessions.get(session_id)
        if not session or not session.session_store:
            return

        self._flush_frontend_buffers(session)
        session.payloads_data = deepcopy(payloads)

        # 序列化 checkpoint 数据
        checkpoint_data: list[dict] = []
        if session.checkpoint_manager:
            checkpoint_data = [cp.to_dict() for cp in session.checkpoint_manager._checkpoints]

        data = SessionData(
            session_id=session.id,
            working_directory=session.working_directory,
            title=title if title is not None else session._title,
            created_at=session.created_at,
            last_active_at=time.time(),
            message_count=message_count if message_count is not None else 0,
            phase=session.phase,
            project_context=project_context or session._resume_project_context,
            payloads=payloads,
            linked_directories=linked_directories if linked_directories is not None else session.linked_directories,
            usage_total=session.usage_total,
            solo_mode=session.solo_mode,
            solo_model=session.solo_model,
            main_model=session.main_model,
            auto_review_enabled=session.auto_review_enabled,
            yolo_mode=session.yolo_mode,
            checkpoints=checkpoint_data,
            coder_payloads=coder_payloads,
            timeline=deepcopy(session.timeline_events),
            conversation_markers=deepcopy(session.conversation_markers),
        )
        await session.session_store.save(session_id, data)

    async def persist_session_metadata(self, session_id: str) -> None:
        """仅持久化会话元数据，避免覆盖已保存的 payloads。"""
        session = self._sessions.get(session_id)
        if not session or not session.session_store:
            return

        self._flush_frontend_buffers(session)

        data = await session.session_store.load(session_id)
        if data is None:
            data = SessionData(
                session_id=session.id,
                working_directory=session.working_directory,
                title=session._title,
                created_at=session.created_at,
                project_context=session._resume_project_context,
                payloads=deepcopy(session.payloads_data),
                linked_directories=list(session.linked_directories),
                usage_total=deepcopy(session.usage_total),
                solo_mode=session.solo_mode,
                solo_model=session.solo_model,
                main_model=session.main_model,
                auto_review_enabled=session.auto_review_enabled,
                yolo_mode=session.yolo_mode,
                checkpoints=[],
                coder_payloads=session._coder_payloads_data,
                timeline=deepcopy(session.timeline_events),
                conversation_markers=deepcopy(session.conversation_markers),
            )
        else:
            data.working_directory = session.working_directory
            data.title = session._title
            data.phase = session.phase
            data.project_context = session._resume_project_context
            data.payloads = deepcopy(session.payloads_data)
            data.linked_directories = list(session.linked_directories)
            data.usage_total = deepcopy(session.usage_total)
            data.solo_mode = session.solo_mode
            data.solo_model = session.solo_model
            data.main_model = session.main_model
            data.auto_review_enabled = session.auto_review_enabled
            data.yolo_mode = session.yolo_mode
            data.coder_payloads = session._coder_payloads_data
            data.timeline = deepcopy(session.timeline_events)
            data.conversation_markers = deepcopy(session.conversation_markers)

        if session.checkpoint_manager:
            data.checkpoints = [
                cp.to_dict() for cp in session.checkpoint_manager._checkpoints
            ]

        await session.session_store.save(session_id, data)

    async def update_session_title(self, session_id: str, title: str) -> None:
        """更新会话标题（内存 + 磁盘）。"""
        session = self._sessions.get(session_id)
        if not session:
            return

        session._title = title

        # 如果有关联的 store 且磁盘有数据，同步更新标题
        if session.session_store:
            data = await session.session_store.load(session_id)
            if data:
                data.title = title
                await session.session_store.save(session_id, data)

    async def list_sessions(self, working_directory: str) -> list[SessionSummary]:
        """列出 working_directory 下的所有历史会话。"""
        store = self._get_store(working_directory)
        return await store.list_all()

    async def delete_session(self, working_directory: str, session_id: str) -> None:
        """删除磁盘上的会话记录。"""
        store = self._get_store(working_directory)
        await store.delete(session_id)

        # 如果正在活跃，也清理内存
        if session_id in self._sessions:
            try:
                await self.interrupt_session(
                    session_id,
                    detail="会话已删除",
                    broadcast_status=False,
                    persist=False,
                )
            except Exception:
                logger.warning(f"删除会话 {session_id[:8]} 时中断活跃任务失败", exc_info=True)
        session = self._sessions.pop(session_id, None)
        if session:
            if session.stream_id:
                self._stream_to_session.pop(session.stream_id, None)
            logger.info(f"已删除会话 {session_id[:8]}")

    async def destroy_session(self, session_id: str) -> None:
        """销毁会话（连接断开时调用）。销毁前先触发一次保存。"""
        session = self._sessions.get(session_id)
        if session is None:
            return

        try:
            await self.interrupt_session(
                session_id,
                detail="会话已关闭",
                broadcast_status=False,
                persist=False,
            )
        except Exception:
            logger.warning(f"销毁会话 {session_id[:8]} 时中断活跃任务失败", exc_info=True)

        self._flush_frontend_buffers(session)
        if session.session_store:
            try:
                await self.persist_session_metadata(session_id)
            except Exception:
                logger.warning(f"销毁会话 {session_id[:8]} 时持久化元数据失败")

        session = self._sessions.pop(session_id, None)
        if session is None:
            return

        if session.permission_manager:
            session.permission_manager.clear_session_rules(session_id)

        if session.stream_id:
            self._stream_to_session.pop(session.stream_id, None)

    def get_session(self, session_id: str) -> CodingSession | None:
        return self._sessions.get(session_id)

    def get_session_ids_by_conn(self, conn_id: str) -> list[str]:
        """返回指定连接关联的所有会话 ID（包括后台运行的）。"""
        return [
            sid for sid, sess in self._sessions.items()
            if sess.conn_id == conn_id
        ]

    def get_session_by_stream_id(self, stream_id: str) -> CodingSession | None:
        sid = self._stream_to_session.get(stream_id)
        return self._sessions.get(sid) if sid else None

    def bind_stream_id(self, session_id: str, stream_id: str) -> None:
        self._stream_to_session[stream_id] = session_id
        session = self._sessions.get(session_id)
        if session:
            session.stream_id = stream_id

    def reset_interrupt(self, session_id: str) -> None:
        """清除会话的中断标记，供下一轮请求重新开始。"""
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.interrupt_requested = False

    def track_runtime_task(self, session_id: str, task: asyncio.Task[Any]) -> None:
        """登记会话运行时 task，便于 interrupt 时统一取消。"""
        session = self._sessions.get(session_id)
        if session is None or task.done():
            return

        session.active_runtime_tasks.add(task)

        def _cleanup(done_task: asyncio.Task[Any]) -> None:
            current = self._sessions.get(session_id)
            if current is not None:
                current.active_runtime_tasks.discard(done_task)

        task.add_done_callback(_cleanup)

    def build_runtime_task_observer(
        self,
        session_id: str,
    ) -> Callable[[asyncio.Task[Any]], None]:
        """构造供工具执行器使用的 task 观察回调。"""
        return lambda task: self.track_runtime_task(session_id, task)

    def register_process(self, session_id: str, process: Any) -> None:
        """登记会话关联的子进程。"""
        session = self._sessions.get(session_id)
        if session is None or process is None:
            return
        key = int(getattr(process, "pid", 0) or id(process))
        session.active_processes[key] = process

    def unregister_process(self, session_id: str, process: Any) -> None:
        """移除会话关联的子进程登记。"""
        session = self._sessions.get(session_id)
        if session is None or process is None:
            return
        key = int(getattr(process, "pid", 0) or id(process))
        session.active_processes.pop(key, None)

    def _abort_pending_approvals(self, session: CodingSession, reason: str) -> None:
        """让所有待审批命令立即结束等待。"""
        for request_id, event in list(session.pending_approvals.items()):
            session.approval_results[request_id] = "deny"
            if reason:
                session.approval_reasons[request_id] = reason
            event.set()

    async def interrupt_session(
        self,
        session_id: str,
        *,
        detail: str = "操作已中断",
        broadcast_status: bool = True,
        persist: bool = True,
    ) -> bool:
        """中断会话当前工作，取消流循环、工具 task 与子进程。"""
        session = self._sessions.get(session_id)
        if session is None:
            return False

        session.interrupt_requested = True
        session.coder_guidance_queue.clear()
        self._abort_pending_approvals(session, detail)

        stream_id = str(session.stream_id or "")
        active_tasks = [
            task for task in list(session.active_runtime_tasks)
            if not task.done()
        ]
        active_processes = list(session.active_processes.values())

        if stream_id:
            try:
                from src.core.transport.distribution.stream_loop_manager import (
                    get_stream_loop_manager,
                )

                await get_stream_loop_manager().stop_stream_loop(stream_id)
            except Exception:
                logger.warning(
                    f"中断会话 {session_id[:8]} 时停止流循环失败",
                    exc_info=True,
                )

        for task in active_tasks:
            task.cancel()

        if active_tasks:
            await asyncio.gather(
                *[
                    self._wait_for_runtime_task_cancel(task)
                    for task in active_tasks
                ],
                return_exceptions=True,
            )

        if active_processes:
            await asyncio.gather(
                *[
                    self._terminate_process(process)
                    for process in active_processes
                ],
                return_exceptions=True,
            )

        session.active_runtime_tasks.clear()
        session.active_processes.clear()
        session.pending_approvals.clear()
        session.approval_results.clear()
        session.approval_prefixes.clear()
        session.approval_reasons.clear()
        session.staging_area = None
        session._coder_payloads_data = None
        session.phase = "ready"

        if persist and session.session_store:
            try:
                await self.persist_session_metadata(session_id)
            except Exception:
                logger.warning(
                    f"中断会话 {session_id[:8]} 时持久化元数据失败",
                    exc_info=True,
                )

        if broadcast_status:
            await self.broadcast_to_session(session_id, {
                "type": "agent.status",
                "payload": {
                    "phase": "ready",
                    "detail": detail,
                    "source": "agent",
                },
            })

        return bool(stream_id or active_tasks or active_processes)

    def cache_payloads_data(self, session_id: str, payloads: list[dict[str, Any]]) -> None:
        """更新会话内存中的序列化 payload 快照。"""
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.payloads_data = deepcopy(payloads)
        session._resume_payloads = deepcopy(payloads)

    async def broadcast_to_session(self, session_id: str, message: dict) -> None:
        """向会话的 WebSocket 发送消息。

        即使当前无 websocket（后台运行中的会话），也会记录 timeline，
        确保切换回来时能恢复完整进度。
        """
        session = self._sessions.get(session_id)
        if not session:
            return
        # 始终记录 timeline，即使没有 websocket（后台会话也需要追踪进度）
        payload_patch = self._record_frontend_event(session, message)
        if not session.websocket:
            # 后台会话：agent.status 的 phase 变化需持久化，使前端自动刷新可见
            if str(message.get("type", "") or "") == "agent.status" and session.session_store:
                asyncio.ensure_future(self.persist_session_metadata(session_id))
            return  # 无 websocket 则不发送，但 timeline 已记录
        outbound = deepcopy(message)
        if payload_patch:
            payload = outbound.get("payload")
            if not isinstance(payload, dict):
                payload = {}
                outbound["payload"] = payload
            payload.update(payload_patch)
        msg = {
            "id": str(uuid4()),
            "session_id": session_id,
            "timestamp": time.time(),
            **outbound,
        }
        try:
            await session.websocket.send(json.dumps(msg, ensure_ascii=False))
        except Exception:
            pass

    async def send_approval_request(
        self, session_id: str, request_id: str, command: str,
        working_dir: str, context: str,
        auto_review_result: dict | None = None,
    ) -> None:
        """发送审批请求到前端。"""
        session = self._sessions.get(session_id)
        if not session:
            return

        event = asyncio.Event()
        session.pending_approvals[request_id] = event

        await self.broadcast_to_session(session_id, {
            "type": "console.approval_request",
            "payload": {
                "request_id": request_id,
                "command": command,
                "working_dir": working_dir,
                "working_directory": working_dir,
                "context": context,
                "auto_review_result": auto_review_result,
            },
        })

    async def wait_for_approval(
        self, session_id: str, request_id: str
    ) -> tuple[str, str, str]:
        """阻塞等待审批结果。返回 (decision, prefix, reason)。"""
        session = self._sessions.get(session_id)
        if not session:
            return ("deny", "", "")

        event = session.pending_approvals.get(request_id)
        if not event:
            return ("deny", "", "")

        try:
            await asyncio.wait_for(event.wait(), timeout=300)
        except asyncio.TimeoutError:
            session.pending_approvals.pop(request_id, None)
            return ("deny", "", "")

        decision = session.approval_results.get(request_id, "deny")
        prefix = session.approval_prefixes.get(request_id, "")
        reason = session.approval_reasons.get(request_id, "")

        session.pending_approvals.pop(request_id, None)
        session.approval_results.pop(request_id, None)
        session.approval_prefixes.pop(request_id, None)
        session.approval_reasons.pop(request_id, None)

        return (decision, prefix, reason)

    async def submit_approval(
        self, session_id: str, request_id: str,
        decision: str, prefix: str = "", reason: str = "",
    ) -> None:
        """前端提交审批结果。"""
        session = self._sessions.get(session_id)
        if not session:
            return

        session.approval_results[request_id] = decision
        session.approval_prefixes[request_id] = prefix
        if reason:
            session.approval_reasons[request_id] = reason

        event = session.pending_approvals.get(request_id)
        if event:
            event.set()

    async def _wait_for_runtime_task_cancel(
        self,
        task: asyncio.Task[Any],
        timeout: float = 3.0,
    ) -> None:
        """等待运行时 task 响应取消。"""
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            logger.warning("等待运行时任务取消超时")
        except Exception:
            pass

    async def _terminate_process(self, process: Any, timeout: float = 3.0) -> None:
        """兜底结束仍未退出的子进程。"""
        if process is None:
            return

        try:
            if getattr(process, "returncode", None) is not None:
                return
        except Exception:
            return

        try:
            process.kill()
        except ProcessLookupError:
            return
        except Exception:
            logger.warning("终止会话子进程失败", exc_info=True)
            return

        wait = getattr(process, "wait", None)
        if not callable(wait):
            return

        try:
            await asyncio.wait_for(wait(), timeout=timeout)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("等待子进程退出失败", exc_info=True)

    def record_user_message(
        self,
        session_id: str,
        content: str,
        *,
        kind: str = "message",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """记录用户消息到可恢复时间线。"""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        clean_content = str(content or "").strip()
        if not clean_content:
            return None
        merged_metadata: dict[str, Any] = {}
        if kind and kind != "message":
            merged_metadata["kind"] = kind
        if metadata:
            merged_metadata.update(deepcopy(metadata))
        return self._append_timeline_message(
            session,
            role="user",
            content=clean_content,
            metadata=merged_metadata or None,
        )

    def record_conversation_marker(
        self,
        session_id: str,
        *,
        anchor_message_id: str,
        kind: str,
        payload_count: int,
        timeline_count: int,
        checkpoint_count: int,
        message_count: int,
        checkpoint_id: str = "",
        title: str = "",
        usage_total: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """记录一个可恢复的会话边界。"""
        session = self._sessions.get(session_id)
        if session is None or not anchor_message_id:
            return None

        marker = {
            "anchor_message_id": anchor_message_id,
            "kind": kind,
            "payload_count": max(int(payload_count), 0),
            "timeline_count": max(int(timeline_count), 0),
            "checkpoint_count": max(int(checkpoint_count), 0),
            "message_count": max(int(message_count), 0),
            "checkpoint_id": checkpoint_id,
            "title": title,
            "usage_total": deepcopy(usage_total if usage_total is not None else session.usage_total),
        }

        replaced = False
        for index, existing in enumerate(session.conversation_markers):
            if (
                str(existing.get("anchor_message_id", "")) == anchor_message_id
                and str(existing.get("kind", "")) == kind
            ):
                session.conversation_markers[index] = marker
                replaced = True
                break
        if not replaced:
            session.conversation_markers.append(marker)

        anchor = self._find_timeline_event(session, anchor_message_id)
        if anchor is not None:
            event_metadata = dict(anchor.get("metadata", {}) or {})
            if kind == "before_user_message":
                if checkpoint_id:
                    event_metadata["checkpoint_id"] = checkpoint_id
                event_metadata["revocable"] = True
            elif kind == "after_agent_message":
                event_metadata["forkable"] = True
            if event_metadata:
                anchor["metadata"] = event_metadata

        return marker

    def get_conversation_marker(
        self,
        session_id: str,
        *,
        anchor_message_id: str,
        kind: str | None = None,
    ) -> dict[str, Any] | None:
        """按锚点消息 ID 查找会话边界。"""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        for marker in session.conversation_markers:
            if str(marker.get("anchor_message_id", "")) != anchor_message_id:
                continue
            if kind and str(marker.get("kind", "")) != kind:
                continue
            return deepcopy(marker)
        return None

    def restore_conversation_to_marker(
        self,
        session_id: str,
        *,
        anchor_message_id: str,
        kind: str,
        payloads_override: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """将会话状态裁剪回指定边界。"""
        session = self._sessions.get(session_id)
        if session is None:
            return None

        marker = self.get_conversation_marker(
            session_id,
            anchor_message_id=anchor_message_id,
            kind=kind,
        )
        if marker is None:
            return None

        payload_source = self._select_payload_snapshot(
            session,
            payloads_override,
            required_count=int(marker.get("payload_count", 0) or 0),
        )
        restored_payloads = deepcopy(
            payload_source[: max(int(marker.get("payload_count", 0) or 0), 0)]
        )
        restored_timeline = deepcopy(
            session.timeline_events[: max(int(marker.get("timeline_count", 0) or 0), 0)]
        )

        session.payloads_data = deepcopy(restored_payloads)
        session._resume_payloads = deepcopy(restored_payloads)
        session.timeline_events = restored_timeline
        session._title = str(marker.get("title", "") or "")
        usage_total = marker.get("usage_total")
        session.usage_total = deepcopy(usage_total) if isinstance(usage_total, dict) else {}
        session.phase = "ready"
        session.stream_buffers.clear()
        session.thinking_buffers.clear()
        session.last_completed_agent_message_ids.clear()
        session.pending_approvals.clear()
        session.approval_results.clear()
        session.approval_prefixes.clear()
        session.approval_reasons.clear()
        session.interrupt_requested = False
        session.active_runtime_tasks.clear()
        session.active_processes.clear()

        checkpoint_count = max(int(marker.get("checkpoint_count", 0) or 0), 0)
        if session.checkpoint_manager:
            session.checkpoint_manager._checkpoints = (
                session.checkpoint_manager._checkpoints[:checkpoint_count]
            )
            session.checkpoint_manager._step_counter = max(
                (cp.step_index for cp in session.checkpoint_manager._checkpoints),
                default=0,
            )

        valid_ids = {
            str(event.get("id", "") or "")
            for event in session.timeline_events
            if str(event.get("id", "") or "")
        }
        session.conversation_markers = [
            deepcopy(existing)
            for existing in session.conversation_markers
            if str(existing.get("anchor_message_id", "") or "") in valid_ids
        ]

        return {
            "payloads": restored_payloads,
            "message_count": max(int(marker.get("message_count", 0) or 0), 0),
            "title": session._title,
        }

    async def fork_session_from_marker(
        self,
        source_session_id: str,
        *,
        conn_id: str,
        anchor_message_id: str,
        payloads_override: list[dict[str, Any]] | None = None,
    ) -> CodingSession | None:
        """从指定 agent 消息边界复制出一个新会话。"""
        session = self._sessions.get(source_session_id)
        if session is None:
            return None

        marker = self.get_conversation_marker(
            source_session_id,
            anchor_message_id=anchor_message_id,
            kind="after_agent_message",
        )
        if marker is None:
            return None

        payload_source = self._select_payload_snapshot(
            session,
            payloads_override,
            required_count=int(marker.get("payload_count", 0) or 0),
        )
        payload_count = max(int(marker.get("payload_count", 0) or 0), 0)
        timeline_count = max(int(marker.get("timeline_count", 0) or 0), 0)
        checkpoint_count = max(int(marker.get("checkpoint_count", 0) or 0), 0)

        payloads = deepcopy(payload_source[:payload_count])
        timeline = deepcopy(session.timeline_events[:timeline_count])
        valid_ids = {
            str(event.get("id", "") or "")
            for event in timeline
            if str(event.get("id", "") or "")
        }
        markers = [
            deepcopy(existing)
            for existing in session.conversation_markers
            if str(existing.get("anchor_message_id", "") or "") in valid_ids
            and int(existing.get("timeline_count", 0) or 0) <= timeline_count
        ]

        checkpoints: list[dict[str, Any]] = []
        if session.checkpoint_manager:
            checkpoints = [
                cp.to_dict()
                for cp in session.checkpoint_manager._checkpoints[:checkpoint_count]
            ]

        new_session_id = str(uuid4())
        fork_title = self._build_fork_title(
            str(marker.get("title", "") or session._title or "")
        )
        usage_total = marker.get("usage_total")
        fork_data = SessionData(
            session_id=new_session_id,
            working_directory=session.working_directory,
            title=fork_title,
            created_at=time.time(),
            last_active_at=time.time(),
            phase="ready",
            project_context=deepcopy(session._resume_project_context),
            payloads=payloads,
            linked_directories=list(session.linked_directories),
            usage_total=deepcopy(usage_total) if isinstance(usage_total, dict) else deepcopy(session.usage_total),
            solo_mode=session.solo_mode,
            solo_model=session.solo_model,
            main_model=session.main_model,
            auto_review_enabled=session.auto_review_enabled,
            yolo_mode=session.yolo_mode,
            checkpoints=checkpoints,
            coder_payloads=None,
            timeline=timeline,
            conversation_markers=markers,
        )

        store = self._get_store(session.working_directory)
        await store.save(new_session_id, fork_data)
        forked, _ = await self.resume_session(
            conn_id=conn_id,
            working_directory=session.working_directory,
            session_id=new_session_id,
        )
        return forked

    def _record_frontend_event(
        self,
        session: CodingSession,
        message: dict[str, Any],
    ) -> dict[str, Any] | None:
        """将发往前端的结构化事件归档到可恢复时间线。"""
        msg_type = str(message.get("type", "") or "")
        payload = message.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}

        if msg_type == "agent.text":
            source = str(payload.get("source", "agent") or "agent")
            chunk = str(payload.get("content", payload.get("text", "")) or "")
            is_final = bool(payload.get("is_final", False))
            buffer = session.stream_buffers.setdefault(
                source,
                {
                    "content": "",
                    "timestamp": int(time.time() * 1000),
                    "metadata": {"source": source},
                },
            )
            buffer_metadata = dict(buffer.get("metadata", {}) or {})
            buffer_metadata["source"] = source
            if "forkable" in payload:
                buffer_metadata["forkable"] = bool(payload.get("forkable"))
            buffer["metadata"] = buffer_metadata
            if chunk:
                existing = str(buffer.get("content", "") or "")
                if is_final and existing and chunk.startswith(existing):
                    buffer["content"] = chunk
                else:
                    buffer["content"] = existing + chunk
            if is_final:
                self._flush_thinking_buffers(session, source)
                flushed = self._flush_stream_buffers(session, source)
                if flushed:
                    return {"message_id": flushed[-1]["id"]}
            return None

        if msg_type == "agent.thinking":
            source = str(payload.get("source", "agent") or "agent")
            content = str(payload.get("content", payload.get("text", "")) or "")
            if not content.strip():
                return
            buffer = session.thinking_buffers.setdefault(
                source,
                {
                    "content": "",
                    "timestamp": int(time.time() * 1000),
                },
            )
            buffer["content"] = content
            return None

        if msg_type == "agent.status":
            if str(payload.get("phase", "") or "") == "ready":
                self._flush_frontend_buffers(session)
            return None

        if msg_type == "tool.call":
            self._flush_frontend_buffers(session)
            name = str(payload.get("name", payload.get("tool_name", "")) or "")
            args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
            args_summary = str(payload.get("args_summary", "") or "")
            stage = str(payload.get("stage", "running") or "running")
            source = str(payload.get("source", "agent") or "agent")
            call_id = str(payload.get("call_id", "") or "")
            self._append_timeline_message(
                session,
                role="system",
                content=args_summary or name,
                metadata={
                    "kind": "tool_call",
                    "call_id": call_id,
                    "tool_name": name,
                    "args": args,
                    "args_summary": args_summary,
                    "stage": stage,
                    "source": source,
                },
            )
            return None

        if msg_type == "checkpoint.created":
            self._flush_frontend_buffers(session)
            self._append_timeline_message(
                session,
                role="system",
                content=str(payload.get("description", "") or ""),
                metadata={
                    "kind": "checkpoint_created",
                    "id": payload.get("id"),
                    "step": payload.get("step"),
                    "tool": payload.get("tool"),
                    "description": payload.get("description"),
                    "files_affected": payload.get("files_affected"),
                    "reversible": payload.get("reversible"),
                },
            )
            return None

        if msg_type == "console.output":
            self._flush_thinking_buffers(session)
            content = str(payload.get("content", payload.get("output", "")) or "")
            if not content:
                return
            self._append_timeline_message(
                session,
                role="system",
                content=content,
                metadata={
                    "kind": "console_output",
                    "exit_code": payload.get("exit_code"),
                    "stream": payload.get("stream", "stdout"),
                },
            )
            return None

        if msg_type == "file.change":
            self._flush_thinking_buffers(session)
            action = str(payload.get("action", payload.get("change_type", "")) or "")
            path = str(payload.get("path", "") or "")
            self._append_timeline_message(
                session,
                role="system",
                content=f"文件变更 [{action or 'modify'}]: {path}".strip(),
                metadata={
                    "kind": "file_change",
                    "change_type": action,
                    "path": path,
                    "diff": payload.get("diff", ""),
                    "content": payload.get("content", ""),
                },
            )
            return None

        if msg_type == "checkpoint.rollback_result":
            self._flush_frontend_buffers(session)
            restored_files = payload.get("restored_files", [])
            warnings = payload.get("warnings", [])
            if not isinstance(restored_files, list):
                restored_files = []
            if not isinstance(warnings, list):
                warnings = []
            self._append_timeline_message(
                session,
                role="system",
                content=(
                    f"回滚完成\n恢复文件: {', '.join(restored_files) or '无'}\n"
                    f"警告: {', '.join(warnings) or '无'}"
                ),
                metadata={"kind": "rollback_result"},
            )
            return None

        if msg_type == "link.result":
            self._flush_frontend_buffers(session)
            status = str(payload.get("status", "") or "")
            success = payload.get("success")
            if success is None:
                success = status in ("ok", "already_linked")
            message_text = str(
                payload.get(
                    "message",
                    "目录关联成功" if success else "目录关联失败",
                ) or ""
            )
            self._append_timeline_message(
                session,
                role="system",
                content=message_text,
                metadata={
                    "kind": "link_result",
                    "success": bool(success),
                    "status": status,
                    "path": payload.get("path"),
                    "project_name": payload.get("project_name"),
                    "virtual_environment": payload.get("virtual_environment"),
                    "research_triggered": payload.get("research_triggered"),
                },
            )
            return None

        if msg_type == "goal.complete":
            self._flush_frontend_buffers(session)
            self._append_timeline_message(
                session,
                role="system",
                content="目标已完成",
                metadata={"kind": "goal_complete"},
            )
            return None

        if msg_type == "agent.context_compressing":
            self._flush_frontend_buffers(session)
            self._append_timeline_message(
                session,
                role="system",
                content="正在压缩上下文...",
                metadata={"kind": "compressing"},
            )
        return None

    def _append_timeline_message(
        self,
        session: CodingSession,
        *,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        timestamp: int | None = None,
    ) -> dict[str, Any] | None:
        """向会话时间线追加一条消息。"""
        text = str(content or "")
        if not text and not metadata:
            return None
        event = {
            "id": str(uuid4()),
            "role": role,
            "content": text,
            "timestamp": timestamp or int(time.time() * 1000),
        }
        if metadata:
            event["metadata"] = deepcopy(metadata)
        session.timeline_events.append(event)
        return event

    def _flush_stream_buffers(
        self,
        session: CodingSession,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """将流式响应缓冲区冲刷为可恢复的 agent 消息。"""
        flushed_events: list[dict[str, Any]] = []
        targets = [source] if source else list(session.stream_buffers.keys())
        for target in targets:
            if not target:
                continue
            buffer = session.stream_buffers.pop(target, None)
            if not buffer:
                continue
            content = str(buffer.get("content", "") or "")
            if not content.strip():
                continue
            timestamp = int(buffer.get("timestamp", int(time.time() * 1000)))
            metadata = dict(buffer.get("metadata", {}) or {})
            metadata["source"] = target
            event = self._append_timeline_message(
                session,
                role="agent",
                content=content,
                metadata=metadata,
                timestamp=timestamp,
            )
            if event is not None:
                session.last_completed_agent_message_ids[target] = str(event["id"])
                flushed_events.append(event)
        return flushed_events

    def _flush_thinking_buffers(
        self,
        session: CodingSession,
        source: str | None = None,
    ) -> None:
        """将 thinking 快照缓冲区冲刷为可恢复的 system 消息。"""
        targets = [source] if source else list(session.thinking_buffers.keys())
        for target in targets:
            if not target:
                continue
            buffer = session.thinking_buffers.pop(target, None)
            if not buffer:
                continue
            content = str(buffer.get("content", "") or "")
            if not content.strip():
                continue
            timestamp = int(buffer.get("timestamp", int(time.time() * 1000)))
            self._append_timeline_message(
                session,
                role="system",
                content=content,
                metadata={
                    "kind": "thinking",
                    "source": target,
                },
                timestamp=timestamp,
            )

    def _flush_frontend_buffers(self, session: CodingSession) -> None:
        """冲刷前端流式缓冲区，保证持久化与时间线顺序稳定。"""
        self._flush_thinking_buffers(session)
        self._flush_stream_buffers(session)

    @staticmethod
    def _build_fork_title(title: str) -> str:
        base = str(title or "").strip() or "未命名会话"
        suffix = " · fork"
        if base.endswith(suffix):
            return base[:60]
        return f"{base}{suffix}"[:60]

    @staticmethod
    def _select_payload_snapshot(
        session: CodingSession,
        override: list[dict[str, Any]] | None,
        *,
        required_count: int,
    ) -> list[dict[str, Any]]:
        candidates = [
            override or [],
            session.payloads_data,
            session._resume_payloads or [],
        ]
        for candidate in candidates:
            if len(candidate) >= required_count:
                return candidate
        return candidates[0]

    @staticmethod
    def _find_timeline_event(
        session: CodingSession,
        message_id: str,
    ) -> dict[str, Any] | None:
        for event in session.timeline_events:
            if str(event.get("id", "") or "") == message_id:
                return event
        return None


# 全局单例
_global_session_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    global _global_session_manager
    if _global_session_manager is None:
        _global_session_manager = SessionManager()
    return _global_session_manager
