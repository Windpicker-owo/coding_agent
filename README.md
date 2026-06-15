# Coding Agent

> 类 Claude Code 的 AI 编程智能体，前后端分离架构，运行在 Neo-MoFox 框架之上。

## 快速上手（5 分钟）

### 1. 确认插件已安装

`plugins/coding_agent/` 和 `plugins/coding_agent_webui/` 目录存在即可，启动时框架会自动加载。

### 2. 配置 5 个模型任务

在 `config/model.toml` 末尾追加以下内容（**这是唯一必须的配置**）：

```toml
# ── Coding Agent 所需模型任务 ──

[model_tasks.coding_main]
model_list = ["你的主力模型名"]       # 主 Agent，负责理解需求、制定计划
max_tokens = 131072

[model_tasks.coding_researcher]
model_list = ["你的性价比模型名"]     # 研究员，并行分析代码模块
max_tokens = 131072

[model_tasks.coding_coder]
model_list = ["你的代码模型名"]       # Coder，精确实施代码变更
max_tokens = 131072

[model_tasks.coding_reviewer]
model_list = ["你的轻量模型名"]       # 自动审查 bash 命令安全性
max_tokens = 500

[model_tasks.coding_title]
model_list = ["你的轻量模型名"]       # 生成会话标题（十几个字）
max_tokens = 50
```

`model_list` 里的名字就是你在 `[[models]]` 段中定义的 `name` 字段。

> **注意** `[[models]]` 中的 `max_context`（上下文窗口大小）也要合理配置，不然会反复触发上下文压缩，大幅降低模型性能

---

## 配置详解

### 模型配置（model.toml）

Coding Agent 通过 **模型任务**（model task）引用模型，而不是直接指定模型名。每个任务是一个独立的配置单元，可以覆盖模型的默认参数。

**模型任务可配置的参数：**

| 参数 | 说明 | 推荐值 |
|------|------|--------|
| `model_list` | 使用的模型名列表 | 根据场景选 |
| `max_tokens` | 最大输出 token 数 | main/coder: 131072, researcher: 131072, reviewer: 500, title: 50 |
| `temperature` | 温度参数 | 默认 0.7，代码任务建议 0.1-0.3 |
| `concurrency_count` | 并发请求数 | 默认 1 |

**5 个任务的职责：**

| 任务名 | 谁来用 | 推荐模型 |
|--------|--------|----------|
| `coding_main` | 主编排器——理解需求、制定计划、调度子 Agent | 强推理 |
| `coding_researcher` | 模块研究员——并行分析项目代码 | 性价比 |
| `coding_coder` | Coder Agent——按计划精确修改代码 | 强代码 |
| `coding_reviewer` | 自动审查员——判断 bash 命令风险 | 轻量 |
| `coding_title` | 标题生成——用首条用户消息生成标题 | 轻量 |

> **5 个任务可以使用同一个模型**，只需要把 5 个 `model_list` 都填成同一个模型名。对于个人使用完全没问题。

### 插件配置（config/plugins/coding_agent/config.toml）

```toml
[model]
main_task = "coding_main"          # 可改，映射到 model.toml 中的任务名
researcher_task = "coding_researcher"
coder_task = "coding_coder"
reviewer_task = "coding_reviewer"
title_task = "coding_title"

[context]
cache_ttl_hours = 24               # 项目研究缓存有效期（小时），0 表示每次重新研究
max_parallel_researchers = 6        # 最大并行研究员数

[bash]
default_timeout = 30               # bash 命令默认超时秒数
max_output_lines = 200             # 最大输出行数
preferred_terminal = "pwsh"        # 优先终端（空 = 自动检测，可设 pwsh/powershell/cmd/bash等）

[ws]
host = "0.0.0.0"
port = 8765
path = "/coding-agent/ws"
tui_username = "User"              # 前端显示的用户名

[mcp]
main_mcp_servers = []              # 主 Agent 可用的 MCP 服务器名列表
coder_mcp_servers = []             # Coder 可用的 MCP 服务器名列表
researcher_mcp_servers = []        # 研究员可用的 MCP 服务器名列表
```

### Coder 模型 Profile（可选）

如果想为 Coder Agent 在不同场景使用不同模型（例如前端用 Claude、后端用 DeepSeek），可以配置 `model_profiles`：

```toml
[[model_profiles]]
profile_name = "claude-architect"
model_name = "claude-sonnet-4"        # model.toml 中 [[models]] 的 name
tags = ["后端", "复杂逻辑", "架构"]
description = "适用于复杂后端和架构设计"
```

主 Agent 会根据任务标签自动选择最合适的 Coder 模型。不配置则始终使用 `coding_coder` 任务指定的模型。

---

## WebUI 使用指南

在开始前，确保您已经安装了mofox code的webui插件。

### 连接

1. 启动后端后，浏览器打开 `http://localhost:8680`
2. 确认 WebSocket 地址正确（默认 `ws://localhost:8765/coding-agent/ws`）
3. 点击「连接」
4. 在弹出的对话框中输入项目目录路径，点击「打开项目」

### 基本操作

- **发送消息**：底部输入框输入，Enter 发送，Shift+Enter 换行
- **工作中追加引导**：Agent 执行工具时，可直接输入补充指令（guidance 模式）
- **中断**：Agent 工作时顶部出现红色「中断」按钮
- **审批 Bash**：弹出审批对话框，可批准/拒绝，可选填 sudo prefix 和原因
- **切换会话**：左侧历史会话列表点击即可切换。**工作中切换会弹出警告**

### 模式开关

| 模式 | 说明 |
|------|------|
| **Auto** | 自动审查 bash 命令，安全系数高的直接放行 |
| **YOLO** | 跳过所有 bash 审批（⚠ 谨慎使用） |
| **Pro / Solo** | 新建会话时可选。Solo 模式用单一模型完成所有工作，不用 create_plan/implement_plan |
| **Goal** | 输入目标后 Agent 离线自主迭代直到完成 |

### 会话管理

- 切换会话时，**旧会话在后台继续运行**，不会中断
- 切回时会自动恢复完整进度
- 每个会话的输入草稿自动保存，切走再回来内容还在
- 侧栏会显示工作中会话的蓝色旋转标记
- 会话列表每 10 秒自动刷新
- 关闭浏览器标签页会清理所有后台会话

---

## 常见问题

### Q: 模型上下文长度 / max_tokens 改了没生效？

模型上下文窗口（`max_context`）在 `[[models]]` 段中定义，Coding Agent 自动读取。你不需要手动改它——内置的上下文压缩会在接近上限时自动裁剪历史。

`max_tokens` 在 `[model_tasks.xxx]` 中定义，控制**单次输出上限**。如果要调，改任务配置而不是改 `[[models]]`。

### Q: 报错 "模型 'xxx' 不存在"？

检查 `model_list` 里填的名字是否与 `[[models]]` 段中某个 `name` 完全一致（区分大小写）。

### Q: WebUI 显示"前端尚未构建"？

进入 `plugins/coding_agent_webui/frontend/` 运行：

```bash
npm install
npm run build
```

### Q: 怎么用自己的 API Key？

在 `config/model.toml` 中添加或修改 `[[api_providers]]`：

```toml
[[api_providers]]
name = "MyProvider"
base_url = "https://api.openai.com/v1"
api_key = "sk-your-key-here"
client_type = "openai"
```

然后在 `[[models]]` 中把 `api_provider` 设为 `"MyProvider"`。

### Q: Agent 执行命令时卡住很久？

通常是 bash 审批超时——弹窗出来了但没注意到，后端默认等 300 秒。遇到卡住直接点「中断」按钮。如果频繁遇到，建议开 YOLO 模式。

---

## 架构

```
┌──────────────┐     WebSocket      ┌────────────────┐
│   WebUI 前端  │ ◄──────────────► │  CodingAgent    │
│              │                    │   Adapter       │
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

### 工作流程

1. **连接**：WebSocket 连接 → 创建 CodingSession → 绑定 ChatStream
2. **项目研究**（首次或缓存过期）：ProjectScoutAgent 侦察结构 → ModuleResearcherAgent 并行分析模块 → 缓存 24 小时
3. **交互循环**：用户输入 → 主 Agent 理解 → 制定计划 → Coder 执行 → 审查
4. **多会话**：支持同时多个会话后台并发工作，切换不中断

---

## 组件一览

| 组件 | 名称 | 说明 |
|------|------|------|
| Chatter | `coding_agent` | 主编排器，对话流程、子 Agent 调度 |
| Adapter | `coding_agent_adapter` | WebSocket 服务端，多连接管理 |
| Agent | `project_scout` | 侦察项目结构、技术栈 |
| Agent | `module_researcher` | 深入分析模块，输出结构化报告 |
| Agent | `coder` | 按实施计划精确修改代码 |
| Agent | `auto_reviewer` | 审查 bash 命令安全性 |
| Tool | `bash` | 执行 shell 命令 |
| Tool | `read` | 读取文件 |
| Tool | `write` | 创建/覆写文件 |
| Tool | `edit` | 精确替换文件片段 |
| Tool | `grep` | 正则搜索内容 |
| Tool | `find` | 按文件名搜索 |
| Tool | `ls` | 列出目录 |
| Tool | `create_plan` | 创建实施计划文档 |
| Tool | `implement_plan` | 将计划交 Coder 实施 |

---

## 目录结构

```
plugins/coding_agent/
├── chatter.py               # 主编排器
├── adapter.py               # WebSocket 适配器
├── orchestration.py         # 子 Agent 编排
├── config.py                # 配置定义
├── prompts.py               # Prompt 模板
├── session_manager.py       # 会话管理
├── session_store.py         # 会话持久化
├── checkpoint_manager.py    # 操作回滚
├── permission_manager.py    # 命令审批规则
├── mcp_integration.py       # MCP 工具注入
├── agents/                  # 子 Agent
│   ├── project_scout.py
│   ├── module_researcher.py
│   ├── coder.py
│   └── auto_reviewer.py
├── tools/                   # 工具集
│   ├── bash.py / read.py / write.py / edit.py
│   ├── grep.py / find.py / ls.py
│   ├── create_plan.py / implement_plan.py
│   └── base.py
└── services/
    ├── project_context.py
    ├── gitignore_scope.py
    ├── terminal_environment.py
    ├── tool_loop_guard.py
    ├── model_router.py
    ├── coder_retry.py
    ├── file_staging.py
    └── skill_loader.py
```

---

## 安全

- **路径沙箱**：文件操作限制在工作目录 + linked_directories 内
- **命令审批**：bash 按规则引擎分级审批（永久规则 → 会话规则 → 用户确认）
- **自动审查**：AutoReviewer 用轻量模型预判命令风险
- **操作回滚**：写操作前自动创建快照

---

## 许可

MoFox Team — 作为 Neo-MoFox 项目的一部分发布。
