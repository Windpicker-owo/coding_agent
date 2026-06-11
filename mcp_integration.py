"""MCP 工具集成模块。

为 coding_agent 的 main / coder / researcher 三个 Agent 提供 MCP 工具注入功能。
参考 default_chatter 的 MCP 注入模式，使用 get_mcp_manager().get_tool_classes_for_servers(names)
获取动态工具类。
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .plugin import CodingAgentPlugin


def get_mcp_tools_for_agent(plugin: CodingAgentPlugin, agent_key: str) -> list[type[Any]]:
    """获取指定 Agent 可用的 MCP 工具类列表。

    Args:
        plugin: Coding Agent 插件实例（用于读取 config.mcp）
        agent_key: Agent 标识（"main" / "coder" / "researcher"）

    Returns:
        MCP 动态工具类列表。空配置或 MCP 未初始化时返回空列表，不会崩溃。
    """
    try:
        config = getattr(plugin, "config", None)
        if config is None:
            return []

        mcp_section = getattr(config, "mcp", None)
        if mcp_section is None:
            return []

        server_names: list[str] = getattr(mcp_section, f"{agent_key}_mcp_servers", None)
        if not server_names:
            return []

        from src.core.managers.tool_manager import get_mcp_manager

        mcp_manager = get_mcp_manager()
        return mcp_manager.get_tool_classes_for_servers(server_names)
    except Exception:
        return []
