"""编程场景专用的上下文压缩实现。

替代 default_chat_context_compression_handler，面向编程场景设计提示词，
总结代码变更、文件修改、计划决策、技术上下文，不需要情感/人格分析。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from src.kernel.llm import LLMRequest, LLMPayload, ROLE, Text
from src.kernel.llm.types import ModelEntry, ModelSet

CONTEXT_COMPRESSION_TIMEOUT_SECONDS = 300.0
CONTEXT_COMPRESSION_MAX_RETRY = 3

CODING_CONTEXT_COMPRESSION_PROMPT = """## 主要提示
你的任务是对一次编程协作对话进行详细摘要，专注于技术上下文和代码实施进展。
你需要保留所有关键的技术决策、代码变更细节、文件操作记录、错误信息和修复策略，
以便在上下文恢复后无缝继续编程工作。

## 分析流程
在提供最终摘要之前，请将你的分析包装在 <analysis> 标签中，以组织你的思路。
在分析过程中：

按时间顺序分析对话中的每条消息。对每条消息深入识别：
- 用户提出的编程任务、需求或问题
- AI 给出的技术方案、代码建议或实施操作
- 实际执行的文件变更（读取、写入、编辑、删除等）
- 执行过程中遇到的错误、警告及其原因与修复方式
- 工具调用的结果与影响
- 架构决策、技术选型及其理由
- 关键代码片段及其用途
- 计划推进状态（已完成哪些步骤，当前在哪个步骤）

不需要分析情感、态度、人格或交流风格。聚焦于技术事实和可操作的上下文。

## 摘要结构
你的摘要应包括以下部分：

### 1. 主要任务
详细描述用户的核心编程任务、目标和预期结果。

### 2. 代码变更
列出所有已执行的文件变更操作：
- 文件路径、变更类型（创建/修改/删除）
- 变更内容摘要（关键代码逻辑、函数签名、类结构）
- 变更原因和背景

### 3. 关键决策
记录重要的技术决策和架构选择：
- 选择的技术方案及理由
- 排除的替代方案
- 技术约束和权衡

### 4. 错误与修复
记录遇到的错误、警告及修复过程：
- 错误信息（完整或关键摘录）
- 根因分析
- 采取的修复措施和验证结果

### 5. 待处理事项
列出所有尚未完成的任务、TODO 项和遗留问题。

### 6. 当前进度
精确描述当前的实施进度：
- 计划中的哪个步骤
- 最近一次操作是什么
- 下一步应该做什么
- 如果有被中断的操作，明确指出中断点和上下文

## 输出格式示例（XML 格式）
<analysis>
  [你的思考过程，确保全面准确地涵盖所有技术要点]
</analysis>

<summary>
  1. 主要任务：
  [详细描述]

  2. 代码变更：
  - [文件路径]: [变更类型] - [变更摘要]
  - [...]

  3. 关键决策：
  - [决策 1]: [理由]
  - [...]

  4. 错误与修复：
  - [错误描述]: [根因] → [修复措施]
  - [...]

  5. 待处理事项：
  - [待处理项 1]
  - [...]

  6. 当前进度：
  [当前步骤、最近操作、下一步计划]

</summary>

## 附加说明
请根据迄今为止的对话提供摘要，遵循此结构并确保技术细节的精确性和全面性。"""


def _clone_models_for_context_compression(model_set: ModelSet) -> ModelSet:
    """为上下文压缩请求生成固定超时和重试配置。"""

    return [
        {
            **model,
            "timeout": CONTEXT_COMPRESSION_TIMEOUT_SECONDS,
            "max_retry": CONTEXT_COMPRESSION_MAX_RETRY,
        }
        for model in model_set
    ]


def _extract_summary_content(raw_text: str) -> str:
    """从模型返回中提取 summary 节点内容。"""

    if not raw_text:
        return ""

    try:
        root = ET.fromstring(f"<root>{raw_text}</root>")
        summary_node = root.find("summary")
        if summary_node is not None:
            summary_text = "".join(summary_node.itertext()).strip()
            if summary_text:
                return summary_text
    except ET.ParseError:
        pass

    match = re.search(r"<summary>(.*?)</summary>", raw_text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return raw_text.strip()


async def _broadcast_compressing(session_id: str, compressing: bool) -> None:
    """广播上下文压缩状态到前端。"""
    if not session_id:
        return

    from .session_manager import get_session_manager

    await get_session_manager().broadcast_to_session(session_id, {
        "type": "agent.context_compressing",
        "payload": {"compressing": compressing},
    })


async def coding_context_compression_handler(
    request: LLMRequest,
    source_payloads: list[LLMPayload],
    model: ModelEntry,
) -> list[LLMPayload]:
    """将超出窗口的编程上下文压缩为单条 user 摘要消息。

    使用编程专用提示词，保留技术上下文和代码实施细节。
    压缩期间会向前端广播 agent.context_compressing 状态。
    """

    del model

    if not source_payloads:
        source_payloads = list(request.payloads)

    if not source_payloads:
        return []

    # 推导 session_id 用于前端通知
    stream_id = request.meta_data.get("stream_id", "")
    session_id = ""
    if stream_id:
        from .session_manager import get_session_manager
        session = get_session_manager().get_session_by_stream_id(stream_id)
        if session:
            session_id = session.id

    compression_request = LLMRequest(
        model_set=_clone_models_for_context_compression(request.model_set),
        request_name=f"{request.request_name}:context_compression",
        clients=request.clients,
        enable_metrics=request.enable_metrics,
    )
    compression_request.context_manager = None
    compression_request.payloads = source_payloads + [
        LLMPayload(ROLE.USER, Text(CODING_CONTEXT_COMPRESSION_PROMPT))
    ]

    await _broadcast_compressing(session_id, True)
    try:
        response = await compression_request.send(auto_append_response=False, stream=False)
    finally:
        await _broadcast_compressing(session_id, False)

    summary_content = _extract_summary_content(response.message or "")
    if not summary_content:
        return []

    compressed_context = (
        "以下是已经压缩过的编程协作历史上下文，"
        "请将其视为此前已经发生的编程对话与技术操作记录，"
        "并在此基础上继续当前编程任务：\n\n"
        f"<summary>\n{summary_content}\n</summary>"
    )
    return [LLMPayload(ROLE.USER, Text(compressed_context))]
