"""Coding Agent 会话持久化存储。

在 .agents/context/sessions/ 下以 JSON 文件保存完整对话上下文，
支持序列化/反序列化 LLMPayload 和各类 Content 类型。
使用原子写入（temp file + os.replace）防止断电损坏。
"""

from __future__ import annotations

import json
import os
import re
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

_GUIDANCE_PREFIX = "【工作中追加引导】\n"
_SYSTEM_REMINDER_PATTERN = re.compile(
    r"<system_reminder>.*?</system_reminder>",
    re.DOTALL | re.MULTILINE,
)
_MESSAGE_METADATA_PREFIX_PATTERN = re.compile(
    r"^\s*(?:【?\d{1,2}:\d{2}(?::\d{2})?】?\s*)?(?:<成员>|【成员】)\s*[^\n]*?[：:]\s*"
)


def _normalize_tool_name(name: str) -> str:
    """去除常见工具名前缀，便于前端展示。"""
    for prefix in ("tool-", "action-", "agent-"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _summarize_tool_args(args: Any) -> str:
    """将工具参数序列化为简短摘要。"""
    if not isinstance(args, dict) or not args:
        return ""
    parts: list[str] = []
    for key, value in args.items():
        value_text = str(value)
        if len(value_text) > 60:
            value_text = value_text[:57] + "..."
        parts.append(f"{key}={value_text!r}")
    summary = ", ".join(parts)
    if len(summary) > 160:
        summary = summary[:157] + "..."
    return summary


def strip_display_prefixes(text: str) -> str:
    """移除仅用于运行时语义的显示前缀。"""
    clean = str(text or "")
    previous = None
    while clean != previous:
        previous = clean
        clean = _MESSAGE_METADATA_PREFIX_PATTERN.sub("", clean, count=1)
        if clean.startswith(_GUIDANCE_PREFIX):
            clean = clean[len(_GUIDANCE_PREFIX):]
        clean = _SYSTEM_REMINDER_PATTERN.sub("", clean)
    return clean.strip()


def build_timeline_from_payloads(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从旧版 payload 历史重建可恢复时间线。

    旧 session 没有单独保存 frontend timeline，但 payloads 中仍包含用户消息、
    assistant 文本以及 ToolCall/ToolResult，可用于回放基本对话轨迹。
    """
    timeline: list[dict[str, Any]] = []
    now_ms = int(time.time() * 1000)

    for payload_index, payload in enumerate(payloads):
        role = str(payload.get("role", "") or "")
        content_items = payload.get("content", [])
        if not isinstance(content_items, list):
            continue

        for item_index, item in enumerate(content_items):
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("__type__", "") or "")
            event_id = f"legacy-{payload_index}-{item_index}"
            timestamp = now_ms + payload_index * 100 + item_index

            if role == "user" and item_type == "Text":
                text = str(item.get("text", "") or "")
                metadata: dict[str, Any] = {}
                if text.startswith(_GUIDANCE_PREFIX):
                    metadata["kind"] = "guidance"
                text = strip_display_prefixes(text)
                if text.strip():
                    event = {
                        "id": event_id,
                        "role": "user",
                        "content": text,
                        "timestamp": timestamp,
                    }
                    if metadata:
                        event["metadata"] = metadata
                    timeline.append(event)
                continue

            if role == "assistant":
                if item_type == "ReasoningText":
                    text = str(item.get("text", "") or "")
                    text = strip_display_prefixes(text)
                    if text.strip():
                        timeline.append({
                            "id": event_id,
                            "role": "system",
                            "content": text,
                            "timestamp": timestamp,
                            "metadata": {
                                "kind": "thinking",
                                "source": "agent",
                            },
                        })
                elif item_type == "Text":
                    text = str(item.get("text", "") or "")
                    text = strip_display_prefixes(text)
                    if text.strip():
                        timeline.append({
                            "id": event_id,
                            "role": "agent",
                            "content": text,
                            "timestamp": timestamp,
                            "metadata": {"source": "agent"},
                        })
                elif item_type == "ToolCall":
                    raw_name = str(item.get("name", "") or "")
                    tool_name = _normalize_tool_name(raw_name)
                    args = item.get("args", {})
                    args_summary = _summarize_tool_args(args)
                    timeline.append({
                        "id": event_id,
                        "role": "system",
                        "content": args_summary or tool_name,
                        "timestamp": timestamp,
                        "metadata": {
                            "kind": "tool_call",
                            "tool_name": tool_name or raw_name or "unknown",
                            "args": args if isinstance(args, dict) else {},
                            "args_summary": args_summary,
                            "stage": "completed",
                            "source": "agent",
                        },
                    })
                continue

            if role == "tool_result" and item_type == "ToolResult":
                value = str(item.get("value", "") or "")
                raw_name = str(item.get("name", "") or "")
                tool_name = _normalize_tool_name(raw_name)
                metadata: dict[str, Any] = {
                    "kind": "tool_result",
                    "tool_name": tool_name or raw_name or "unknown",
                }
                if tool_name == "bash":
                    metadata["kind"] = "bash_output"
                    metadata["stream"] = "stdout"
                timeline.append({
                    "id": event_id,
                    "role": "system",
                    "content": value,
                    "timestamp": timestamp,
                    "metadata": metadata,
                })

    return timeline


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
    usage_total: dict[str, dict[str, Any]] = field(default_factory=dict)  # 按 model_name 累计用量
    solo_mode: bool = False  # Solo 模式：单一 agent 完成所有工作
    solo_model: str = ""  # Solo 模式使用的 LLM task 名
    auto_review_enabled: bool = False  # 自动审查模式
    yolo_mode: bool = False  # 免审批模式
    checkpoints: list[dict[str, Any]] = field(default_factory=list)  # 持久化的 checkpoint 数据
    coder_payloads: list[dict[str, Any]] | None = None  # Coder Agent 执行中的中间状态
    timeline: list[dict[str, Any]] = field(default_factory=list)  # 可恢复的前端消息时间线
    conversation_markers: list[dict[str, Any]] = field(default_factory=list)  # 用户撤回 / fork 锚点


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
                "usage_total": data.usage_total,
                "solo_mode": data.solo_mode,
                "solo_model": data.solo_model,
                "auto_review_enabled": data.auto_review_enabled,
                "yolo_mode": data.yolo_mode,
                "checkpoints": data.checkpoints,
                "coder_payloads": data.coder_payloads,
                "timeline": data.timeline,
                "conversation_markers": data.conversation_markers,
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
            usage_total=raw.get("usage_total", {}),
            solo_mode=raw.get("solo_mode", False),
            solo_model=raw.get("solo_model", ""),
            auto_review_enabled=raw.get("auto_review_enabled", False),
            yolo_mode=raw.get("yolo_mode", False),
            checkpoints=raw.get("checkpoints", []),
            coder_payloads=raw.get("coder_payloads"),
            timeline=raw.get("timeline", []),
            conversation_markers=raw.get("conversation_markers", []),
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
        for f in self._sessions_dir.glob("*.json"):
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

        summaries.sort(key=lambda s: s.last_active_at, reverse=True)
        return summaries

    async def delete(self, session_id: str) -> None:
        """删除会话文件。"""
        path = self._session_path(session_id)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
