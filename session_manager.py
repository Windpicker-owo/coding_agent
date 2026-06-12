"""Coding Agent 会话管理器。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from uuid import uuid4

from src.kernel.logger import get_logger

from .checkpoint_manager import CheckpointManager
from .permission_manager import PermissionManager
from .session_store import SessionData, SessionStore, SessionSummary

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

        session = CodingSession(
            id=session_id,
            conn_id=conn_id,
            working_directory=working_directory,
            checkpoint_manager=CheckpointManager(session_id, working_directory),
            permission_manager=perm_mgr,
            session_store=store,
            _resume_payloads=data.payloads,
            _resume_project_context=data.project_context,
            _title=data.title,
            created_at=data.created_at,
            linked_directories=data.linked_directories,
            phase="ready",  # 恢复后直接进入 ready
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
    ) -> None:
        """保存当前会话状态到磁盘。"""
        session = self._sessions.get(session_id)
        if not session or not session.session_store:
            return

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
        )
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

        # 触发一次 fire-and-forget 保存（不阻塞）
        if session.session_store and session._resume_payloads is None:
            # 只有非恢复模式才保存（恢复模式说明还没执行过，没有新数据）
            pass  # 实际保存由 chatter 的 _auto_save 负责

        session = self._sessions.pop(session_id, None)
        if session is None:
            return

        if session.permission_manager:
            session.permission_manager.clear_session_rules(session_id)

        if session.stream_id:
            self._stream_to_session.pop(session.stream_id, None)

    def get_session(self, session_id: str) -> CodingSession | None:
        return self._sessions.get(session_id)

    def get_session_by_stream_id(self, stream_id: str) -> CodingSession | None:
        sid = self._stream_to_session.get(stream_id)
        return self._sessions.get(sid) if sid else None

    def bind_stream_id(self, session_id: str, stream_id: str) -> None:
        self._stream_to_session[stream_id] = session_id
        session = self._sessions.get(session_id)
        if session:
            session.stream_id = stream_id

    async def broadcast_to_session(self, session_id: str, message: dict) -> None:
        """向会话的 WebSocket 发送消息。"""
        session = self._sessions.get(session_id)
        if not session or not session.websocket:
            return
        msg = {
            "id": str(uuid4()),
            "session_id": session_id,
            "timestamp": time.time(),
            **message,
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
            "type": "bash.approval_request",
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


# 全局单例
_global_session_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    global _global_session_manager
    if _global_session_manager is None:
        _global_session_manager = SessionManager()
    return _global_session_manager
