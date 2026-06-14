"""操作回滚系统 - Checkpoint 管理器。"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4


@dataclass
class FileSnapshot:
    """单个文件的修改前快照。"""
    path: str
    action: Literal["create", "modify", "delete"]
    original_content: bytes | None  # None 表示文件原本不存在
    original_mode: int | None

    def to_dict(self) -> dict:
        """序列化为可 JSON 化的字典。"""
        import base64
        return {
            "path": self.path,
            "action": self.action,
            "original_content_b64": base64.b64encode(self.original_content).decode() if self.original_content else None,
            "original_mode": self.original_mode,
        }

    @staticmethod
    def from_dict(data: dict) -> FileSnapshot:
        """从字典反序列化。"""
        import base64
        content_b64 = data.get("original_content_b64")
        return FileSnapshot(
            path=data["path"],
            action=data["action"],
            original_content=base64.b64decode(content_b64) if content_b64 else None,
            original_mode=data.get("original_mode"),
        )


@dataclass
class Checkpoint:
    """一次操作的完整快照。"""
    id: str
    step_index: int
    tool_name: str
    description: str
    file_snapshots: list[FileSnapshot]
    timestamp: float
    agent_name: str
    is_reversible: bool
    bash_command: str | None = None

    def to_dict(self) -> dict:
        """序列化为可 JSON 化的字典。"""
        return {
            "id": self.id,
            "step_index": self.step_index,
            "tool_name": self.tool_name,
            "description": self.description,
            "file_snapshots": [s.to_dict() for s in self.file_snapshots],
            "timestamp": self.timestamp,
            "agent_name": self.agent_name,
            "is_reversible": self.is_reversible,
            "bash_command": self.bash_command,
        }

    @staticmethod
    def from_dict(data: dict) -> Checkpoint:
        """从字典反序列化。"""
        return Checkpoint(
            id=data["id"],
            step_index=data["step_index"],
            tool_name=data["tool_name"],
            description=data["description"],
            file_snapshots=[FileSnapshot.from_dict(s) for s in data.get("file_snapshots", [])],
            timestamp=data["timestamp"],
            agent_name=data["agent_name"],
            is_reversible=data["is_reversible"],
            bash_command=data.get("bash_command"),
        )


@dataclass
class RollbackResult:
    """回滚结果。"""
    rolled_back_checkpoints: list[str]
    restored_files: list[str]
    warnings: list[str]


# 只读命令前缀
_READONLY_PREFIXES = [
    "cat", "head", "tail", "less", "more", "grep", "rg", "find",
    "ls", "tree", "wc", "file", "stat", "diff", "cmp",
    "git log", "git diff", "git status", "git branch", "git show",
    "git blame", "git tag", "git remote", "git shortlog",
    "echo", "printf", "date", "whoami", "hostname", "uname",
    "python -c", "python3 -c", "node -e",
]


class CheckpointManager:
    """管理会话的 Checkpoint 快照与回滚。

    每个 CodingSession 持有一个 CheckpointManager 实例。
    """

    def __init__(self, session_id: str, working_directory: str) -> None:
        self._session_id = session_id
        self._working_dir = working_directory
        self._checkpoints: list[Checkpoint] = []
        self._step_counter: int = 0
        self._git_available: bool = (Path(working_directory) / ".git").is_dir()

    async def snapshot_before_write(
        self, target_path: str, agent_name: str, description: str,
        tool_name: str = "write",
    ) -> Checkpoint:
        """write/edit 操作前快照目标文件。"""
        self._step_counter += 1
        path = Path(target_path)

        snapshots: list[FileSnapshot] = []
        if path.exists():
            try:
                content = path.read_bytes()
                mode = path.stat().st_mode
                snapshots.append(FileSnapshot(
                    path=str(path),
                    action="modify",
                    original_content=content,
                    original_mode=mode,
                ))
            except OSError:
                snapshots.append(FileSnapshot(
                    path=str(path),
                    action="modify",
                    original_content=None,
                    original_mode=None,
                ))
        else:
            snapshots.append(FileSnapshot(
                path=str(path),
                action="create",
                original_content=None,
                original_mode=None,
            ))

        checkpoint = Checkpoint(
            id=str(uuid4()),
            step_index=self._step_counter,
            tool_name=tool_name,
            description=description,
            file_snapshots=snapshots,
            timestamp=time.time(),
            agent_name=agent_name,
            is_reversible=True,
        )
        self._checkpoints.append(checkpoint)
        return checkpoint

    async def snapshot_before_bash(
        self, command: str, agent_name: str
    ) -> Checkpoint:
        """bash 操作前创建快照。"""
        self._step_counter += 1

        is_readonly = self._is_readonly_command(command)

        if is_readonly:
            is_reversible = True
            snapshots: list[FileSnapshot] = []
        elif self._git_available:
            is_reversible = True
            snapshots = []
        else:
            is_reversible = False
            snapshots = []

        checkpoint = Checkpoint(
            id=str(uuid4()),
            step_index=self._step_counter,
            tool_name="bash",
            description=f"bash: {command[:80]}",
            file_snapshots=snapshots,
            timestamp=time.time(),
            agent_name=agent_name,
            is_reversible=is_reversible,
            bash_command=command,
        )
        self._checkpoints.append(checkpoint)
        return checkpoint

    async def snapshot_before_user_message(
        self,
        content: str,
        agent_name: str = "user",
    ) -> Checkpoint:
        """用户消息发送前创建逻辑检查点。

        该检查点不直接保存文件快照，只作为“回到这条用户消息之前”
        的锚点，供后续撤回用户消息时联动回滚后续改动。
        """
        self._step_counter += 1
        summary = " ".join(str(content or "").strip().split())
        if len(summary) > 80:
            summary = summary[:77] + "..."

        checkpoint = Checkpoint(
            id=str(uuid4()),
            step_index=self._step_counter,
            tool_name="user_message",
            description=f"用户消息: {summary or '(空消息)'}",
            file_snapshots=[],
            timestamp=time.time(),
            agent_name=agent_name,
            is_reversible=True,
        )
        self._checkpoints.append(checkpoint)
        return checkpoint

    async def record_bash_file_changes(self, checkpoint_id: str) -> None:
        """bash 执行后检测文件变更并补充快照。"""
        checkpoint = None
        for cp in self._checkpoints:
            if cp.id == checkpoint_id:
                checkpoint = cp
                break

        if checkpoint is None:
            return

        if self._git_available:
            try:
                import asyncio
                process = await asyncio.create_subprocess_exec(
                    "git", "diff", "--name-only",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self._working_dir,
                )
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
                changed_files = stdout.decode("utf-8", errors="replace").strip().splitlines()

                for file_path in changed_files:
                    full_path = Path(self._working_dir) / file_path
                    if full_path.exists():
                        try:
                            content = full_path.read_bytes()
                            mode = full_path.stat().st_mode
                        except OSError:
                            content = None
                            mode = None
                        checkpoint.file_snapshots.append(FileSnapshot(
                            path=str(full_path),
                            action="modify",
                            original_content=content,
                            original_mode=mode,
                        ))
            except Exception:
                pass

    async def rollback_last(self) -> RollbackResult:
        """回滚最后一个 checkpoint。"""
        if not self._checkpoints:
            return RollbackResult(
                rolled_back_checkpoints=[],
                restored_files=[],
                warnings=["没有可回滚的操作"],
            )
        last = self._checkpoints[-1]
        result = await self._rollback_checkpoint(last)
        self._checkpoints.pop()
        return result

    async def rollback_to(self, checkpoint_id: str) -> RollbackResult:
        """回滚到指定 checkpoint（含此 checkpoint 之后的所有操作）。"""
        target_idx = None
        for i, cp in enumerate(self._checkpoints):
            if cp.id == checkpoint_id:
                target_idx = i
                break

        if target_idx is None:
            return RollbackResult(
                rolled_back_checkpoints=[],
                restored_files=[],
                warnings=[f"未找到 checkpoint: {checkpoint_id}"],
            )

        all_rolled_back: list[str] = []
        all_restored: list[str] = []
        all_warnings: list[str] = []

        # 从最新到 target 逐个回滚
        for i in range(len(self._checkpoints) - 1, target_idx - 1, -1):
            cp = self._checkpoints[i]
            result = await self._rollback_checkpoint(cp)
            all_rolled_back.extend(result.rolled_back_checkpoints)
            all_restored.extend(result.restored_files)
            all_warnings.extend(result.warnings)

        # 截断列表
        self._checkpoints = self._checkpoints[:target_idx]

        return RollbackResult(
            rolled_back_checkpoints=all_rolled_back,
            restored_files=all_restored,
            warnings=all_warnings,
        )

    async def _rollback_checkpoint(self, cp: Checkpoint) -> RollbackResult:
        """回滚单个 checkpoint：逐文件还原。

        对于 bash checkpoint 且 git 可用时，优先使用 git checkout 恢复，
        避免因 snapshot 保存时机导致的内容不一致。
        """
        restored: list[str] = []
        warnings: list[str] = []

        # bash checkpoint + git 可用：直接用 git checkout 恢复文件
        if cp.tool_name == "bash" and self._git_available and cp.file_snapshots:
            import asyncio as _asyncio
            for snapshot in cp.file_snapshots:
                file_path = snapshot.path
                try:
                    proc = await _asyncio.create_subprocess_exec(
                        "git", "checkout", "--", file_path,
                        stdout=_asyncio.subprocess.DEVNULL,
                        stderr=_asyncio.subprocess.PIPE,
                        cwd=self._working_dir,
                    )
                    _, stderr = await _asyncio.wait_for(
                        proc.communicate(), timeout=10,
                    )
                    if proc.returncode == 0:
                        restored.append(file_path)
                    else:
                        err_msg = (
                            stderr.decode("utf-8", errors="replace").strip()
                            if stderr else "unknown error"
                        )
                        warnings.append(
                            f"git checkout {file_path} 失败: {err_msg}"
                        )
                except Exception as e:
                    warnings.append(f"git checkout {file_path} 异常: {e}")
            return RollbackResult(
                rolled_back_checkpoints=[cp.id],
                restored_files=restored,
                warnings=warnings,
            )

        # 非 bash 或非 git：走逐文件手动恢复
        for snapshot in cp.file_snapshots:
            path = Path(snapshot.path)
            try:
                if snapshot.action == "create":
                    # 文件是新建的，删除它
                    if path.exists():
                        path.unlink()
                        restored.append(str(path))
                elif snapshot.action == "modify":
                    # 文件被修改了，恢复原内容
                    if snapshot.original_content is not None:
                        path.write_bytes(snapshot.original_content)
                        if snapshot.original_mode is not None:
                            try:
                                os.chmod(path, snapshot.original_mode)
                            except OSError:
                                pass
                        restored.append(str(path))
                    else:
                        warnings.append(f"无法还原 {path}：原始内容不可用")
                elif snapshot.action == "delete":
                    # 文件被删除了，恢复它
                    if snapshot.original_content is not None:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(snapshot.original_content)
                        if snapshot.original_mode is not None:
                            try:
                                os.chmod(path, snapshot.original_mode)
                            except OSError:
                                pass
                        restored.append(str(path))
                    else:
                        warnings.append(f"无法还原 {path}：原始内容不可用")
            except OSError as e:
                warnings.append(f"还原 {path} 失败: {e}")

        return RollbackResult(
            rolled_back_checkpoints=[cp.id],
            restored_files=restored,
            warnings=warnings,
        )

    def list_checkpoints(self) -> list[dict]:
        """返回所有 checkpoint 的摘要字典列表。"""
        return [
            {
                "id": cp.id,
                "step": cp.step_index,
                "tool": cp.tool_name,
                "description": cp.description,
                "files_affected": len(cp.file_snapshots),
                "reversible": cp.is_reversible,
                "timestamp": cp.timestamp,
                "agent": cp.agent_name,
            }
            for cp in self._checkpoints
        ]

    @classmethod
    def from_checkpoint_dicts(
        cls, session_id: str, working_directory: str, data: list[dict],
    ) -> CheckpointManager:
        """从持久化的 checkpoint 字典列表恢复。"""
        mgr = cls(session_id, working_directory)
        mgr._checkpoints = [Checkpoint.from_dict(d) for d in data]
        mgr._step_counter = max(
            (cp.step_index for cp in mgr._checkpoints), default=0,
        )
        return mgr

    @staticmethod
    def _is_readonly_command(command: str) -> bool:
        """判断是否只读命令。"""
        cmd = command.strip()
        for prefix in _READONLY_PREFIXES:
            if cmd.startswith(prefix):
                return True
        return False
