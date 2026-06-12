"""命令安全自动审查 Agent。"""

from __future__ import annotations

from typing import Annotated

import json_repair
from src.app.plugin_system.base import BaseAgent
from src.core.config import get_model_config
from src.kernel.llm import LLMPayload, ROLE, Text

from ..prompts import AUTO_REVIEWER_PROMPT, render_prompt
from ..services.terminal_environment import get_preferred_terminal_from_config


class AutoReviewAgent(BaseAgent):
    """使用小模型判断 bash 命令安全性。"""

    agent_name = "auto_reviewer"
    agent_description = "使用小模型自动审查 bash 命令安全性"
    chatter_allow = ["coding_agent"]
    associated_types = ["text"]
    usables = []  # 无工具

    async def execute(
        self,
        command: Annotated[str, "待审查的 bash 命令"],
        working_directory: Annotated[str, "命令执行目录"],
        task_context: Annotated[str, "当前任务上下文摘要"],
    ) -> tuple[bool, dict]:
        """审查命令安全性。"""
        model_set = get_model_config().get_task("coding_reviewer")
        terminal_environment = get_preferred_terminal_from_config(
            getattr(self.plugin, "config", None)
        )

        request = self.create_llm_request(model_set, "auto_review")
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(
            render_prompt(AUTO_REVIEWER_PROMPT, terminal_environment=terminal_environment)
        )))
        request.add_payload(LLMPayload(ROLE.USER, Text(
            f"命令: {command}\n目录: {working_directory}\n上下文: {task_context}"
        )))

        response = await request.send(stream=False)
        await response

        message = getattr(response, "message", "") or ""

        # 解析 JSON 结果
        try:
            parsed = json_repair.repair_json(message, return_objects=True)
            if isinstance(parsed, dict):
                return True, {
                    "safe": bool(parsed.get("safe", False)),
                    "reason": str(parsed.get("reason", "")),
                    "confidence": float(parsed.get("confidence", 0)),
                }
        except Exception:
            pass

        # 无法解析时默认不安全
        return False, {"safe": False, "reason": "无法解析审查结果", "confidence": 0.0}
