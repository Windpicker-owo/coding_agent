"""Coding Agent 插件入口。"""

from __future__ import annotations

from src.app.plugin_system.base import BasePlugin
from src.core.components.loader import register_plugin

from .config import CodingAgentConfig
from .chatter import CodingAgentChatter
from .adapter import CodingAgentAdapter
from .services.project_context import ProjectContextService
from .tools import (
    BashTool, ReadTool, WriteTool, EditTool,
    GrepTool, FindTool, LsTool,
)
from .agents import (
    ProjectScoutAgent, ModuleResearcherAgent,
    CoderAgent, AutoReviewAgent,
)

@register_plugin
class CodingAgentPlugin(BasePlugin):
    """Coding Agent 插件。"""

    plugin_name = "coding_agent"
    plugin_description = "类 Claude Code 的编程智能体，前后端分离架构"
    plugin_version = "0.1.0"

    configs = [CodingAgentConfig]

    def get_components(self) -> list[type]:
        return [
            # Chatter
            CodingAgentChatter,
            # Adapter
            CodingAgentAdapter,
            # Service
            ProjectContextService,
            # Tools
            BashTool, ReadTool, WriteTool, EditTool,
            GrepTool, FindTool, LsTool,
            # Agents
            ProjectScoutAgent, ModuleResearcherAgent,
            CoderAgent, AutoReviewAgent,
        ]
