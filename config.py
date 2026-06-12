"""Coding Agent 插件配置。"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


class CoderModelProfile(SectionBase):
    """Coder Agent 模型 Profile，定义一组可选的模型路由参数。

    用于 Main Agent 根据任务特征（标签）为 Coder Agent 选择最合适的模型。
    """

    profile_name: str = Field(default="", description="Profile 标识名，如 claude-architect")
    model_name: str = Field(default="", description="model.toml 中的模型名称，如 claude-sonnet-4")
    tags: list[str] = Field(default_factory=list, description="任务特征标签，如 ['后端', '复杂逻辑']")
    description: str = Field(default="", description="Profile 适用场景描述")
    temperature: float | None = Field(default=None, description="覆盖温度参数，None 使用模型默认值")
    max_tokens: int | None = Field(default=None, description="覆盖最大 token 数，None 使用模型默认值")


class CodingAgentConfig(BaseConfig):
    """Coding Agent 配置。"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "Coding Agent 配置"

    @config_section("model", title="模型任务映射", tag="ai")
    class ModelSection(SectionBase):
        """模型任务映射。"""
        main_task: str = Field(default="coding_main", description="主 agent 模型任务名")
        researcher_task: str = Field(default="coding_researcher", description="研究员模型任务名")
        coder_task: str = Field(default="coding_coder", description="编码员模型任务名")
        reviewer_task: str = Field(default="coding_reviewer", description="自动审查模型任务名")

    @config_section("context", title="上下文管理", tag="ai")
    class ContextSection(SectionBase):
        """上下文管理。"""
        cache_ttl_hours: int = Field(default=24, description="项目理解缓存有效期（小时）")
        max_parallel_researchers: int = Field(default=6, description="最大并行研究员数")

    @config_section("bash", title="Bash 工具", tag="advanced")
    class BashSection(SectionBase):
        """Bash 工具配置。"""
        default_timeout: int = Field(default=30, description="默认超时秒数")
        max_output_lines: int = Field(default=200, description="最大输出行数")
        preferred_terminal: str = Field(default="", description="优先使用的终端环境，如 pwsh、powershell、cmd、bash")

    @config_section("ws", title="WebSocket 适配器", tag="network")
    class WsSection(SectionBase):
        """WebSocket 适配器配置。"""
        host: str = Field(default="0.0.0.0", description="监听地址")
        port: int = Field(default=8765, description="监听端口")
        path: str = Field(default="/coding-agent/ws", description="WebSocket 路径")
        tui_username: str = Field(default="TUI User", description="TUI 客户端显示的用户名")

    @config_section("mcp", title="MCP 工具集成", tag="ai")
    class MCPSection(SectionBase):
        """MCP 工具集成。"""
        main_mcp_servers: list[str] = Field(default_factory=list, description="主Agent可用的MCP服务器名")
        coder_mcp_servers: list[str] = Field(default_factory=list, description="Coder可用的MCP服务器名")
        researcher_mcp_servers: list[str] = Field(default_factory=list, description="研究员可用的MCP服务器名")

    model: ModelSection = Field(default_factory=ModelSection)
    context: ContextSection = Field(default_factory=ContextSection)
    bash: BashSection = Field(default_factory=BashSection)
    ws: WsSection = Field(default_factory=WsSection)
    mcp: MCPSection = Field(default_factory=MCPSection)
    model_profiles: list[CoderModelProfile] = Field(
        default_factory=list,
        description="Coder Agent 可选模型 Profile 列表，Main Agent 可据此选择模型",
    )
