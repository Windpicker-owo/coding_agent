# Coding Agent

> 类 Claude Code 的编程智能体，前后端分离架构。运行在 Neo-MoFox 框架之上。

## 概述

Coding Agent 是一个 AI 编程助手插件，能够理解你的项目、制定实施计划、精确修改代码。它通过 **WebSocket** 与前端 TUI 通信，支持流式对话、工具调用、会话持久化和操作回滚。

### 核心特性

- **子代理协作**：主编排器 + 4 个专职子代理（侦察 / 研究 / 编码 / 审查）
- **流式响应**：LLM 输出实时推送到前端
- **会话持久化**：对话历史保存到 `.agents/context/sessions/`，重启后无缝恢复
- **操作回滚**：每次写操作自动创建快照，支持一键回滚
- **命令审批**：危险的 bash 命令需用户确认，支持永久规则和会话规则
- **MCP 集成**：可通过 MCP 协议扩展工具能力
- **项目上下文缓存**：自动侦察项目结构，缓存 24 小时避免重复分析

## 架构

```
┌──────────────┐     WebSocket      ┌────────────────┐
│   TUI 前端    │ ◄──────────────► │  CodingAgent    │
│  (独立项目)   │                    │   Adapter       │
└──────────────┘                    └───────┬────────┘
                                            │ 消息管线
                                   ┌────────▼────────┐
                                   │  CodingAgent     │
                                   │   Chatter        │
                                   │  (主编排器)       │
                                   └───┬──────┬──────┘
                                       │      │
                          ┌────────────┤      ├────────────┐
                          ▼            ▼      ▼            ▼
                   ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
                   │  Scout   │ │Researcher│ │  Coder   │ │Reviewer  │
                   │  (侦察)  │ │ (研究)   │ │ (编码)   │ │ (审查)   │
                   └──────────┘ └──────────┘ └──────────┘ └──────────┘
                          │            │           │
                          └────────────┼───────────┘
                                       │
                              ┌────────▼────────┐
                              │     工具层       │
                              │ bash/read/write │
                              │ edit/grep/find  │
                              │ ls/create_plan  │
                              │ implement_plan  │
                              └─────────────────┘
```

### 组件一览

| 组件类型 | 组件名 | 说明 |
|---------|--------|------|
| Chatter | `coding_agent` | 主编排器，管理对话流程、子代理调度 |
| Adapter | `coding_agent_adapter` | WebSocket 服务端，管理 TUI 连接 |
| Agent | `project_scout` | 快速侦察项目结构、技术栈、模块边界 |
| Agent | `module_researcher` | 深入分析指定模块，输出结构化报告 |
| Agent | `coder` | 按落地计划精确实施代码变更 |
| Agent | `auto_reviewer` | 用轻量模型自动审查 bash 命令安全性 |
| Tool | `bash` | 执行 shell 命令，支持终端环境选择和审批 |
| Tool | `read` | 读取文件内容 |
| Tool | `write` | 创建/覆写文件 |
| Tool | `edit` | 精确替换文件片段 |
| Tool | `grep` | 正则搜索文件内容 |
| Tool | `find` | 按文件名/模式搜索文件 |
| Tool | `ls` | 列出目录结构 |
| Tool | `create_plan` | 创建实施计划文档 |
| Tool | `implement_plan` | 将计划交给 Coder Agent 实施 |
| Service | `project_context` | 项目上下文缓存的读写 |

## 工作流程

### 1. 连接阶段

TUI 通过 WebSocket 连接 `ws://host:8765/coding-agent/ws`，指定工作目录。Adapter 创建 `CodingSession` 并绑定到 `ChatStream`。

### 2. 项目研究（Phase 1）

首次进入项目（或缓存过期）时：

1. **ProjectScoutAgent** 快速侦察：目录结构、技术栈、配置、构建系统等
2. 根据侦察结果确定需要深入研究的模块
3. **ModuleResearcherAgent** 并行研究各模块（默认最多 6 个并行）
4. 研究结果缓存到 `.agents/context/project_overview.json`（默认 TTL 24 小时）

### 3. 交互循环（Phase 2）

```
用户输入 → 主编排器理解需求 → 制定计划 → Coder执行 → 审查结果
```

- 主编排器负责理解需求、澄清歧义、制定实施计划
- Coder Agent 按照计划精确修改代码
- AutoReviewer 审查 bash 命令的安全性
- 所有操作记录到 Checkpoint，支持回滚

### 4. 会话恢复

重启后可通过 `session_id` 恢复之前的对话，完整回放历史消息。

## 配置

```toml
# config.toml 中的 coding_agent 配置段

[plugins.coding_agent.config.model]
main_task = "coding_main"          # 主 agent 模型
researcher_task = "coding_researcher"  # 研究员模型
coder_task = "coding_coder"        # 编码员模型
reviewer_task = "coding_reviewer"  # 审查员模型

[plugins.coding_agent.config.context]
cache_ttl_hours = 24               # 项目缓存有效期
max_parallel_researchers = 6        # 最大并行研究员

[plugins.coding_agent.config.bash]
default_timeout = 30               # bash 默认超时秒数
max_output_lines = 200             # 最大输出行数
preferred_terminal = "pwsh"        # 优先终端 (pwsh/powershell/cmd/bash)

[plugins.coding_agent.config.ws]
host = "0.0.0.0"
port = 8765
path = "/coding-agent/ws"

[plugins.coding_agent.config.mcp]
main_mcp_servers = []              # 主 Agent 可用的 MCP 服务器
coder_mcp_servers = []             # Coder 可用的 MCP 服务器
researcher_mcp_servers = []        # 研究员可用的 MCP 服务器
```

## 首次使用

需要在 `config/model.toml` 中配置 **5 个模型任务**，插件才能正常工作：

| 任务名 | 用途 | 推荐模型类型 |
|--------|------|-------------|
| `coding_main` | 主编排器（理解需求、制定计划） | 强推理模型 |
| `coding_researcher` | 模块研究员（分析代码） | 性价比模型 |
| `coding_coder` | 编码员（实施代码变更） | 强推理模型 |
| `coding_reviewer` | 命令安全审查 | 轻量模型 |
| `coding_title` | 会话标题生成 | 轻量模型 |

**最简配置示例**（追加到 `model.toml` 末尾）：

```toml
[model_tasks.coding_main]
model_list = ["你的强推理模型名"]

[model_tasks.coding_researcher]
model_list = ["你的性价比模型名"]

[model_tasks.coding_coder]
model_list = ["你的强推理|代码模型名"]

[model_tasks.coding_reviewer]
model_list = ["你的轻量模型名"]

[model_tasks.coding_title]
model_list = ["你的轻量模型名"]
```

> 模型名需与你在 `[[api_providers]]` 中配置的提供商兼容。每个任务还可按需配置 `max_tokens`、`temperature`、`concurrency_count` 等参数。

## 目录结构

```
plugins/coding_agent/
├── __init__.py              # 插件入口
├── plugin.py                # 插件注册
├── chatter.py               # 主编排器（699行）
├── adapter.py               # WebSocket 适配器（849行）
├── orchestration.py         # 子代理编排器
├── config.py                # 配置定义
├── prompts.py               # 所有 Prompt 模板
├── session_manager.py       # 会话管理
├── session_store.py         # 会话持久化
├── checkpoint_manager.py    # 操作回滚系统
├── permission_manager.py    # 命令审批规则引擎
├── mcp_integration.py       # MCP 工具注入
│
├── agents/                  # 子代理
│   ├── project_scout.py     # 项目侦察
│   ├── module_researcher.py # 模块研究
│   ├── coder.py             # 代码实施
│   └── auto_reviewer.py     # 命令安全审查
│
├── tools/                   # 工具集
│   ├── base.py              # 工具共享基类
│   ├── bash.py              # Shell 执行
│   ├── read.py              # 文件读取
│   ├── write.py             # 文件写入
│   ├── edit.py              # 精确编辑
│   ├── grep.py              # 内容搜索
│   ├── find.py              # 文件查找
│   ├── ls.py                # 目录列表
│   ├── create_plan.py       # 创建计划
│   └── implement_plan.py    # 实施计划
│
└── services/                # 服务
    ├── project_context.py   # 项目上下文缓存
    ├── gitignore_scope.py   # .gitignore 过滤
    ├── terminal_environment.py  # 终端环境适配
    └── tool_loop_guard.py   # 工具循环护栏
```

## 依赖

- **Neo-MoFox Core** ≥ 1.2.0-rc
- `json_repair`：容错 JSON 解析
- 运行中的模型服务（OpenAI/Anthropic 兼容接口）

## 前端（TUI）

Coding Agent 本身只包含后端逻辑，TUI 前端为独立项目，通过 WebSocket 协议连接。协议消息格式可参考 `adapter.py` 中的 `broadcast_to_session` 和消息处理逻辑。

## 安全

- **路径沙箱**：所有文件操作限制在项目工作目录及 `linked_directories` 内
- **命令审批**：bash 命令按规则引擎分级审批（永久允许/拒绝 → 会话允许 → 需用户确认）
- **自动审查**：AutoReviewer 用轻量模型预判命令风险
- **操作回滚**：写操作前自动创建快照，误操作可回滚

## 许可

MoFox Team - 作为 Neo-MoFox 项目的一部分发布。
