"""Coding Agent 会话持久化存储。

在 .agents/context/sessions/ 下以 JSON 文件保存完整对话上下文，
支持序列化/反序列化 LLMPayload 和各类 Content 类型。
使用原子写入（temp file + os.replace）防止断电损坏。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.kernel.llm.payload import (
    LLMPayload,
    ReasoningText,
    Text,
    ToolCall,
    ToolResult,
)
from src.kernel.llm.payload.tooling import LLMUsable
from src.kernel.llm.roles import ROLE

# ── 序列化 / 反序列化 ──────────────────────────────────────────────


def serialize_content(content_item: Any) -> dict[str, Any]:
    """将单个 Content 对象序列化为 dict。"""
    if isinstance(content_item, Text):
        return {"__type__": "Text", "text": content_item.text}
    if isinstance(content_item, ReasoningText):
        result: dict[str, Any] = {"__type__": "ReasoningText", "text": content_item.text}
        if content_item.signature:
            result["signature"] = content_item.signature
        if content_item.redacted_data:
            result["redacted_data"] = content_item.redacted_data
        return result
    if isinstance(content_item, ToolCall):
        return {
            "__type__": "ToolCall",
            "id": content_item.id,
            "name": content_item.name,
            "args": content_item.args,
        }
    if isinstance(content_item, ToolResult):
        return {
            "__type__": "ToolResult",
            "value": content_item.value,
            "call_id": content_item.call_id,
            "name": content_item.name,
        }
    # LLMUsable 类型（TOOL payload 中的工具类）
    if isinstance(content_item, type) and issubclass(content_item, LLMUsable):
        return {"__type__": "LLMUsable", "schema": content_item.to_schema()}

    raise TypeError(f"无法序列化的 Content 类型: {type(content_item).__name__}")


def deserialize_content(data: dict[str, Any]) -> Any:
    """将 dict 反序列化为 Content 对象。"""
    t = data["__type__"]
    if t == "Text":
        return Text(data["text"])
    if t == "ReasoningText":
        return ReasoningText(
            text=data.get("text", ""),
            signature=data.get("signature"),
            redacted_data=data.get("redacted_data"),
        )
    if t == "ToolCall":
        return ToolCall(
            id=data.get("id"),
            name=data.get("name", ""),
            args=data.get("args", {}),
        )
    if t == "ToolResult":
        return ToolResult(
            value=data.get("value"),
            call_id=data.get("call_id"),
            name=data.get("name"),
        )
    if t == "LLMUsable":
        # TOOL payload 在恢复时由 _build_clean_request 重新生成，此处留空
        return None

    raise TypeError(f"未知的 Content 类型: {t}")


def serialize_payload(payload: LLMPayload) -> dict[str, Any]:
    """将 LLMPayload 序列化为 dict。"""
    return {
        "role": payload.role.value,
        "content": [serialize_content(c) for c in payload.content],
    }


def deserialize_payload(data: dict[str, Any]) -> LLMPayload:
    """将 dict 反序列化为 LLMPayload。"""
    role = ROLE(data["role"])
    content: list[Any] = []
    for item in data.get("content", []):
        deserialized = deserialize_content(item)
        if deserialized is not None:
            content.append(deserialized)
    return LLMPayload(role, content)


# ── 数据模型 ──────────────────────────────────────────────────────


@dataclass
class SessionData:
    """完整的会话持久化数据。"""

    session_id: str
    working_directory: str
    title: str = ""
    created_at: float = 0.0
    last_active_at: float = 0.0
    message_count: int = 0
    phase: str = "init"
    project_context: dict[str, Any] | None = None
    payloads: list[dict[str, Any]] = field(default_factory=list)
    linked_directories: list[str] = field(default_factory=list)  # 关联的外部项目目录


@dataclass
class SessionSummary:
    """会话摘要（用于列表展示），只含元数据不含 payloads。"""

    session_id: str
    title: str
    created_at: float
    last_active_at: float
    message_count: int
    phase: str


# ── 持久化服务 ────────────────────────────────────────────────────


class SessionStore:
    """管理单个工作目录下的会话 JSON 文件。"""

    def __init__(self, working_directory: str) -> None:
        self._sessions_dir = Path(working_directory) / ".agents" / "context" / "sessions"

    def _session_path(self, session_id: str) -> Path:
        return self._sessions_dir / f"{session_id}.json"

    async def save(self, session_id: str, data: SessionData) -> None:
        """原子写入会话数据。"""
        self._sessions_dir.mkdir(parents=True, exist_ok=True)

        data.last_active_at = time.time()
        data.message_count = len([
            p for p in data.payloads
            if p.get("role") in ("user", "assistant")
        ])

        content = json.dumps(
            {
                "session_id": data.session_id,
                "working_directory": data.working_directory,
                "title": data.title,
                "created_at": data.created_at,
                "last_active_at": data.last_active_at,
                "message_count": data.message_count,
                "phase": data.phase,
                "project_context": data.project_context,
                "payloads": data.payloads,
                "linked_directories": data.linked_directories,
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        )

        target = self._session_path(session_id)
        # 原子写入：先写临时文件，再 os.replace
        tmp_fd, tmp_path = tempfile.mkstemp(
            suffix=".json", prefix="session_", dir=str(self._sessions_dir)
        )
        try:
            with open(tmp_fd, "w", encoding="utf-8", closefd=True) as f:
                f.write(content)
            os.replace(tmp_path, target)
        except Exception:
            # 清理临时文件
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    async def load(self, session_id: str) -> SessionData | None:
        """加载会话数据。"""
        path = self._session_path(session_id)
        if not path.exists():
            return None

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        return SessionData(
            session_id=raw.get("session_id", session_id),
            working_directory=raw.get("working_directory", ""),
            title=raw.get("title", ""),
            created_at=raw.get("created_at", 0.0),
            last_active_at=raw.get("last_active_at", 0.0),
            message_count=raw.get("message_count", 0),
            phase=raw.get("phase", "init"),
            project_context=raw.get("project_context"),
            payloads=raw.get("payloads", []),
            linked_directories=raw.get("linked_directories", []),
        )

    async def update_title(self, session_id: str, title: str) -> None:
        """仅更新会话标题（原地修改 JSON，避免完整重写）。"""
        path = self._session_path(session_id)
        if not path.exists():
            return

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["title"] = title
            # 原子写入
            tmp_fd, tmp_path = tempfile.mkstemp(
                suffix=".json", prefix="session_", dir=str(self._sessions_dir)
            )
            try:
                with open(tmp_fd, "w", encoding="utf-8", closefd=True) as f:
                    json.dump(raw, f, indent=2, ensure_ascii=False, default=str)
                os.replace(tmp_path, path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except (json.JSONDecodeError, OSError):
            pass

    async def list_all(self) -> list[SessionSummary]:
        """列出所有会话摘要（按 last_active_at 倒序）。"""
        if not self._sessions_dir.exists():
            return []

        summaries: list[SessionSummary] = []
        for f in sorted(self._sessions_dir.glob("*.json"), key=os.path.getmtime, reverse=True):
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
                summaries.append(SessionSummary(
                    session_id=raw.get("session_id", f.stem),
                    title=raw.get("title", ""),
                    created_at=raw.get("created_at", 0.0),
                    last_active_at=raw.get("last_active_at", 0.0),
                    message_count=raw.get("message_count", 0),
                    phase=raw.get("phase", "init"),
                ))
            except (json.JSONDecodeError, OSError):
                continue

        return summaries

    async def delete(self, session_id: str) -> None:
        """删除会话文件。"""
        path = self._session_path(session_id)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
