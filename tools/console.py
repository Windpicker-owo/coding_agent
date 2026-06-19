"""系统终端命令执行工具（含审批流）。"""

from __future__ import annotations

import asyncio
import locale
import subprocess
import sys
import uuid

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
from typing import Annotated

from src.app.plugin_system.base import BaseTool
from src.kernel.logger import get_logger

from .base import CodingToolMixin
from ..services.terminal_environment import build_terminal_launch, get_preferred_terminal_from_config

logger = get_logger("coding_agent.bash")


def _hidden_subprocess_kwargs() -> dict[str, object]:
    """构造 Windows 下隐藏控制台窗口所需的启动参数。"""

    if sys.platform != "win32":
        return {}

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": _CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def _detect_console_encoding() -> str:
    """动态检测控制台编码。

    优先使用 sys.stdout.encoding，再 fallback 到 locale.getpreferredencoding()，
    最后使用 utf-8。Win10 1809+ 默认终端已是 UTF-8。
    """
    for candidate in (
        getattr(sys.stdout, "encoding", None),
        locale.getpreferredencoding(),
    ):
        if candidate and candidate.lower() not in ("", "none"):
            return candidate
    return "utf-8"


_CONSOLE_ENCODING = _detect_console_encoding()
# 模块级缓存：假设进程生命周期内终端编码不变。
# 在极少数场景下（如运行时切换终端）可能过期，但 _execute_command
# 仍可正确解码，因为 subprocess 输出编码由 OS 终端决定，通常稳定。


class ConsoleTool(CodingToolMixin, BaseTool):
    """在当前系统终端环境中执行命令。需要用户审批。"""

    tool_name = "console"
    tool_description = (
        "Execute a command in the current system terminal environment. "
        "Commands require user approval unless pre-approved."
    )
    chatter_allow = ["coding_agent"]

    async def execute(
        self,
        command: Annotated[str, "The command to execute in the terminal"],
        timeout: Annotated[int, "Timeout seconds, 0 = use default"] = 0,
    ) -> tuple[bool, str]:
        """执行终端命令，含审批流程和流式输出。"""
        session = self._get_current_session()
        if session is not None:
            self._ensure_not_interrupted()
        if session is None:
            # 没有活跃会话，直接执行（内部调用场景）
            return await self._execute_command(command, timeout)

        # yolo 模式：跳过所有审批，直接执行（依然走 checkpoint 确保可回滚）
        if session.yolo_mode:
            return await self._execute_with_checkpoint(command, timeout, session)

        session_mgr = self._get_session_manager()
        perm_mgr = session.permission_manager

        # 检查权限
        if perm_mgr:
            check_result = perm_mgr.check_permission(session.id, command)

            from ..permission_manager import PermissionCheckResult

            if check_result == PermissionCheckResult.DENIED:
                return True, f"命令已被拒绝执行: {command}"

            if check_result == PermissionCheckResult.ALLOWED:
                return await self._execute_with_checkpoint(command, timeout, session)

            # NEEDS_APPROVAL - 尝试自动审查
            if session.auto_review_enabled:
                auto_result = await self._auto_review(command, session)
                if auto_result and auto_result.get("safe") and auto_result.get("confidence", 0) >= 0.8:
                    await session_mgr.broadcast_to_session(session.id, {
                        "type": "agent.status",
                        "payload": {
                            "phase": "coding",
                            "detail": (
                                "自动审查已通过，直接执行 bash: "
                                f"{self._summarize_command(command)}"
                            ),
                            "source": "agent",
                        },
                    })
                    return await self._execute_with_checkpoint(command, timeout, session)

            # 发送审批请求到前端
            request_id = str(uuid.uuid4())
            working_dir = self._get_working_directory()
            context = "Coding Agent 请求执行命令"

            await session_mgr.send_approval_request(
                session.id, request_id, command, working_dir, context,
                auto_review_result=auto_result if session.auto_review_enabled else None,
            )

            # 等待审批
            decision, prefix, reason = await session_mgr.wait_for_approval(session.id, request_id)

            from ..permission_manager import ApprovalDecision

            if decision == ApprovalDecision.DENY.value or decision == "deny":
                msg = "用户拒绝执行此命令"
                if reason:
                    msg += f" — {reason}"
                return True, msg

            if decision == ApprovalDecision.ALLOW_SESSION.value or decision == "allow_session":
                if perm_mgr and prefix:
                    perm_mgr.add_session_rule(session.id, prefix)

            if decision == ApprovalDecision.ALLOW_FOREVER.value or decision == "allow_forever":
                if perm_mgr and prefix:
                    perm_mgr.add_forever_rule(prefix, True)

        # 执行命令
        self._ensure_not_interrupted()
        return await self._execute_with_checkpoint(command, timeout, session)

    async def _execute_with_checkpoint(self, command: str, timeout: int, session: object) -> tuple[bool, str]:
        """带 checkpoint 的命令执行。"""
        checkpoint_mgr = self._get_checkpoint_manager()
        checkpoint = None
        if checkpoint_mgr:
            agent_name = getattr(self, "agent_name", "coding_agent")
            checkpoint = await checkpoint_mgr.snapshot_before_console(command, agent_name)

        success, output = await self._execute_command(command, timeout)

        # 记录 console 文件变更
        if checkpoint_mgr and checkpoint:
            await checkpoint_mgr.record_console_file_changes(checkpoint.id)
            await self._notify_checkpoint_created(checkpoint)

        return success, output

    async def _execute_command(self, command: str, timeout: int) -> tuple[bool, str]:
        """实际执行命令。"""
        work_dir = self._get_working_directory()
        plugin_config = getattr(self.plugin, "config", None)
        bash_config = getattr(plugin_config, "bash", None) if plugin_config is not None else None
        preferred_terminal = get_preferred_terminal_from_config(plugin_config)

        # 获取超时配置
        timeout_val = timeout
        if timeout_val <= 0:
            timeout_val = int(getattr(bash_config, "default_timeout", 30) or 30)

        max_output_lines = int(getattr(bash_config, "max_output_lines", 200) or 200)
        launch = build_terminal_launch(command, preferred_terminal)
        # 每次执行时重新检测编码，避免模块加载时的缓存过期
        # （虽然进程生命周期内终端编码通常不变，但防御性检测成本极低）
        output_encoding = _detect_console_encoding()
        process = None
        session = self._get_current_session()
        session_mgr = self._get_session_manager() if session else None
        if preferred_terminal and launch is None:
            logger.warning(f"优先终端不可用，回退到默认 shell: {preferred_terminal}")

        try:
            self._ensure_not_interrupted()
            if launch is None:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.DEVNULL,
                    cwd=work_dir,
                    **_hidden_subprocess_kwargs(),
                )
            else:
                argv, output_encoding = launch
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.DEVNULL,
                    cwd=work_dir,
                    **_hidden_subprocess_kwargs(),
                )
        except OSError as e:
            return False, f"无法启动命令: {e}"

        if session and session_mgr and process is not None:
            session_mgr.register_process(session.id, process)

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout_val
            )
        except asyncio.TimeoutError:
            await self._terminate_process_tree(process)
            return False, f"命令超时（{timeout_val}秒）: {command}"
        except asyncio.CancelledError:
            await asyncio.shield(self._terminate_process_tree(process))
            raise
        finally:
            if session and session_mgr and process is not None:
                session_mgr.unregister_process(session.id, process)

        stdout_text = stdout.decode(output_encoding, errors="replace") if stdout else ""
        stderr_text = stderr.decode(output_encoding, errors="replace") if stderr else ""

        # 截断输出
        stdout_lines = stdout_text.splitlines()
        stderr_lines = stderr_text.splitlines()

        if len(stdout_lines) > max_output_lines:
            stdout_text = "\n".join(stdout_lines[:max_output_lines])
            stdout_text += f"\n... (stdout 已截断，共 {len(stdout_lines)} 行)"

        if len(stderr_lines) > max_output_lines:
            stderr_text = "\n".join(stderr_lines[:max_output_lines])
            stderr_text += f"\n... (stderr 已截断，共 {len(stderr_lines)} 行)"

        # 通知前端 bash 输出
        session = self._get_current_session()
        return_code = process.returncode
        if session:
            mgr = self._get_session_manager()
            if stdout_text:
                await mgr.broadcast_to_session(session.id, {
                    "type": "console.output",
                    "payload": {"stream": "stdout", "content": stdout_text, "is_final": True, "exit_code": return_code},
                })
            if stderr_text:
                await mgr.broadcast_to_session(session.id, {
                    "type": "console.output",
                    "payload": {"stream": "stderr", "content": stderr_text, "is_final": True, "exit_code": return_code},
                })

        output_parts = []
        if stdout_text:
            output_parts.append(stdout_text)
        if stderr_text:
            output_parts.append(f"[stderr]\n{stderr_text}")

        output = "\n".join(output_parts) if output_parts else "(无输出)"

        if return_code == 0:
            return True, output
        else:
            return False, f"命令退出码 {return_code}:\n{output}"

    @staticmethod
    async def _terminate_process_tree(process: object) -> None:
        """尽可能终止命令对应的整个进程树。"""
        if process is None:
            return

        try:
            if getattr(process, "returncode", None) is not None:
                return
        except Exception:
            return

        pid = getattr(process, "pid", None)
        if pid and sys.platform == "win32":
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/T",
                    "/F",
                    "/PID",
                    str(pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    **_hidden_subprocess_kwargs(),
                )
                await asyncio.wait_for(killer.wait(), timeout=3.0)
                return
            except Exception:
                pass

        try:
            process.kill()
        except ProcessLookupError:
            return
        except Exception:
            return

        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except Exception:
            pass

    async def _auto_review(self, command: str, session: object) -> dict | None:
        """调用自动审查 Agent。"""
        try:
            from ..agents.auto_reviewer import AutoReviewAgent
            working_dir = self._get_working_directory()
            reviewer = AutoReviewAgent(
                stream_id=getattr(self, "stream_id", ""),
                plugin=self.plugin,
            )
            success, result = await reviewer.execute(
                command=command,
                working_directory=working_dir,
                task_context="Coding Agent 请求执行 bash 命令",
            )
            if success and isinstance(result, dict):
                return result
        except Exception as e:
            logger.warning(f"自动审查失败: {e}")
        return None

    @staticmethod
    def _summarize_command(command: str, limit: int = 72) -> str:
        """压缩命令摘要，避免状态栏过长。"""
        normalized = " ".join(command.strip().split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3] + "..."
