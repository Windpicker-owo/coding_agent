"""Coding Agent 插件入口。"""

from __future__ import annotations

from src.app.plugin_system.base import BasePlugin
from src.app.plugin_system.api.prompt_api import get_or_create
from src.core.components.loader import register_plugin

from .config import CodingAgentConfig
from .chatter import CodingAgentChatter
from .adapter import CodingAgentAdapter
from .services.project_context import ProjectContextService
from .tools import (
    ConsoleTool, ReadTool, WriteTool, EditTool,
    GrepTool, FindTool, LsTool,
    EnterPhaseTool,
)
from .agents import (
    ProjectScoutAgent, ModuleResearcherAgent,
    CoderAgent, AutoReviewAgent,
)
from .prompts import (
    MAIN_AGENT_SYSTEM_PROMPT,
    SOLO_AGENT_SYSTEM_PROMPT,
    PROJECT_SCOUT_PROMPT,
    MODULE_RESEARCHER_PROMPT,
    CODER_AGENT_PROMPT,
    AUTO_REVIEWER_PROMPT,
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
            ConsoleTool, ReadTool, WriteTool, EditTool,
            GrepTool, FindTool, LsTool,
            EnterPhaseTool,
            # Agents
            ProjectScoutAgent, ModuleResearcherAgent,
            CoderAgent, AutoReviewAgent,
        ]

    async def on_plugin_loaded(self) -> None:
        """注册所有 PromptTemplate。"""
        get_or_create("coding_agent.main_agent", MAIN_AGENT_SYSTEM_PROMPT)
        get_or_create("coding_agent.solo_agent", SOLO_AGENT_SYSTEM_PROMPT)
        get_or_create("coding_agent.project_scout", PROJECT_SCOUT_PROMPT)
        get_or_create("coding_agent.module_researcher", MODULE_RESEARCHER_PROMPT)
        get_or_create("coding_agent.coder", CODER_AGENT_PROMPT)
        get_or_create("coding_agent.auto_reviewer", AUTO_REVIEWER_PROMPT)
