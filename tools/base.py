"""Coding Agent 工具共享基类。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..checkpoint_manager import CheckpointManager
    from ..session_manager import CodingSession, SessionManager


class CodingToolMixin:
    """所有 Coding Agent 工具的共享辅助方法。

    工具类同时继承 BaseTool 和本 Mixin。
    运行时通过 _bind_runtime_context() 注入 stream_id，
    再通过 stream_id 从全局 SessionManager 获取 session。
    """

    def _get_session_manager(self) -> SessionManager:
        """获取全局 SessionManager 单例。"""
        from ..session_manager import get_session_manager
        return get_session_manager()

    def _get_current_session(self) -> CodingSession | None:
        """获取当前 stream_id 对应的 CodingSession。"""
        stream_id = getattr(self, "stream_id", "") or ""
        if not stream_id:
            return None
        mgr = self._get_session_manager()
        return mgr.get_session_by_stream_id(stream_id)

    def _get_checkpoint_manager(self) -> CheckpointManager | None:
        """获取当前会话的 CheckpointManager。"""
        session = self._get_current_session()
        if session is None:
            return None
        return session.checkpoint_manager

    def _get_staging_area(self) -> Any:
        """获取当前会话的文件暂存区（FileStagingArea | None）。"""
        session = self._get_current_session()
        if session is None:
            return None
        return session.staging_area

    def _observe_staging_path(self, path: Path) -> None:
        """将显式访问的文件路径登记到暂存区追踪范围。"""
        staging = self._get_staging_area()
        if staging is None:
            return
        try:
            staging.observe_path(str(path.resolve()))
        except Exception:
            return

    def _get_working_directory(self) -> str:
        """获取当前工作目录。"""
        session = self._get_current_session()
        if session and session.working_directory:
            return session.working_directory
        return str(Path.cwd())

    def _ensure_not_interrupted(self) -> None:
        """若会话已被中断，则立刻终止当前工具执行。"""
        session = self._get_current_session()
        if session is not None and session.interrupt_requested:
            raise asyncio.CancelledError("会话已中断")

    def _resolve_path(self, path: str) -> Path:
        """解析路径：相对路径基于工作目录。"""
        p = Path(path)
        if p.is_absolute():
            return p.resolve()
        return (Path(self._get_working_directory()) / path).resolve()

    def _check_path_safety(self, path: str) -> None:
        """检查路径是否在项目工作目录或关联目录内。越界时 raise ValueError。"""
        resolved = self._resolve_path(path)
        
        # 首先检查主工作目录
        work_dir = Path(self._get_working_directory()).resolve()
        try:
            resolved.relative_to(work_dir)
            return  # 在主工作目录内，允许访问
        except ValueError:
            pass  # 不在主工作目录内，继续检查关联目录
        
        # 检查关联的外部项目目录
        session = self._get_current_session()
        if session and session.linked_directories:
            for linked_dir in session.linked_directories:
                linked_path = Path(linked_dir).resolve()
                try:
                    resolved.relative_to(linked_path)
                    return  # 在关联目录内，允许访问
                except ValueError:
                    continue
        
        # 都不在允许范围内，拒绝访问
        raise ValueError(
            f"路径 {resolved} 不在工作目录 {work_dir} 或关联目录内，拒绝访问"
        )

    async def _notify_file_change(
        self, path: str, action: str, diff: str = "", content: str = ""
    ) -> None:
        """通过 WebSocket 通知前端文件变更。"""
        session = self._get_current_session()
        if session is None:
            return
        mgr = self._get_session_manager()
        await mgr.broadcast_to_session(session.id, {
            "type": "file.change",
            "payload": {"path": path, "action": action, "diff": diff, "content": content},
        })

    async def _notify_checkpoint_created(self, checkpoint: Any) -> None:
        """通知前端 checkpoint 已创建。"""
        session = self._get_current_session()
        if session is None:
            return
        mgr = self._get_session_manager()
        await mgr.broadcast_to_session(session.id, {
            "type": "checkpoint.created",
            "payload": {
                "id": checkpoint.id,
                "step": checkpoint.step_index,
                "tool": checkpoint.tool_name,
                "description": checkpoint.description,
                "files_affected": len(checkpoint.file_snapshots),
                "reversible": checkpoint.is_reversible,
            },
        })
