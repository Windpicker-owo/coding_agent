"""工具调用续轮的可见性护栏。"""

from __future__ import annotations


def advance_silent_tool_rounds(
    current_count: int,
    *,
    chunk_count: int,
    reasoning_content: str | None,
    next_tool_call_count: int,
) -> int:
    """根据本轮可见输出情况更新连续静默工具轮次数。"""
    has_visible_follow_up = bool(
        chunk_count > 0
        or (reasoning_content and reasoning_content.strip())
    )

    if next_tool_call_count > 0 and not has_visible_follow_up:
        return current_count + 1
    return 0