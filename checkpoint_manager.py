"""操作回滚系统 - Checkpoint 管理器。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4


_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


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
    console_command: str | None = None

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
            "console_command": self.console_command,
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
            console_command=data.get("console_command", data.get("bash_command")),
        )


@dataclass
class RollbackResult:
    """回滚结果。"""
    rolled_back_checkpoints: list[str]
    restored_files: list[str]
    warnings: list[str]


@dataclass
class _ConsoleFileState:
    """console checkpoint 的命令前文件状态。"""

    path: str
    exists: bool
    content: bytes | None
    mode: int | None


# 只读命令前缀。
# 这里必须保守：宁可把可回滚的命令误判为“可能写入”，也不能把真实会写文件的命令
# 误判为只读，否则会跳过命令前快照，导致恢复时丢失用户已有的工作区状态。
_READONLY_PREFIXES = [
    "cat", "head", "tail", "less", "more", "grep", "rg", "find",
    "ls", "tree", "wc", "file", "stat", "diff", "cmp",
    "git log", "git diff", "git status", "git branch", "git show",
    "git blame", "git tag", "git remote", "git shortlog",
    "date", "whoami", "hostname", "uname", "pwd",
]

_READONLY_BLOCKERS = [
    ">",
    "out-file",
    "set-content",
    "add-content",
    "tee-object",
    "tee ",
    "python -c",
    "python3 -c",
    "node -e",
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
        self._console_baselines: dict[str, dict[str, _ConsoleFileState]] = {}

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

    async def snapshot_before_console(
        self, command: str, agent_name: str
    ) -> Checkpoint:
        """console 操作前创建快照。"""
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
            tool_name="console",
            description=f"console: {command[:80]}",
            file_snapshots=snapshots,
            timestamp=time.time(),
            agent_name=agent_name,
            is_reversible=is_reversible,
            console_command=command,
        )
        self._checkpoints.append(checkpoint)
        if self._git_available and not is_readonly:
            self._console_baselines[checkpoint.id] = await self._capture_console_baseline()
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

    async def record_console_file_changes(self, checkpoint_id: str) -> None:
        """console 执行后检测文件变更并补充快照。"""
        checkpoint = None
        for cp in self._checkpoints:
            if cp.id == checkpoint_id:
                checkpoint = cp
                break

        if checkpoint is None:
            return

        if self._git_available:
            try:
                baseline = self._console_baselines.pop(checkpoint_id, {})
                checkpoint.file_snapshots = await self._build_console_snapshots(baseline)
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
        if result.warnings:
            result.warnings.append(
                f"检查点 {last.id} 未完全恢复，已保留以便重试"
            )
        else:
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
        truncate_to = len(self._checkpoints)

        # 从最新到 target 逐个回滚
        for i in range(len(self._checkpoints) - 1, target_idx - 1, -1):
            cp = self._checkpoints[i]
            result = await self._rollback_checkpoint(cp)
            all_rolled_back.extend(result.rolled_back_checkpoints)
            all_restored.extend(result.restored_files)
            all_warnings.extend(result.warnings)
            if result.warnings:
                all_warnings.append(
                    f"回滚在检查点 {cp.id} 处中止，未完全恢复的检查点已保留"
                )
                truncate_to = i + 1
                break
            truncate_to = i

        # 截断列表
        self._checkpoints = self._checkpoints[:truncate_to]

        return RollbackResult(
            rolled_back_checkpoints=all_rolled_back,
            restored_files=all_restored,
            warnings=all_warnings,
        )

    async def _rollback_checkpoint(self, cp: Checkpoint) -> RollbackResult:
        """回滚单个 checkpoint：逐文件还原。"""
        restored: list[str] = []
        warnings: list[str] = []

        # 统一走逐文件手动恢复，避免 console 使用 git checkout 误伤已有脏文件。
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
        cmd = command.strip().lower()
        if not cmd:
            return True
        for blocker in _READONLY_BLOCKERS:
            if blocker in cmd:
                return False
        for prefix in _READONLY_PREFIXES:
            if cmd.startswith(prefix):
                return True
        return False

    async def _capture_console_baseline(self) -> dict[str, _ConsoleFileState]:
        """捕获命令执行前工作区中已脏路径的真实状态。"""
        modified, deleted, untracked = await self._git_list_dirty_paths()
        baseline: dict[str, _ConsoleFileState] = {}
        for rel_path in sorted(modified | deleted | untracked):
            baseline[rel_path] = self._read_console_file_state(
                self._absolute_from_relative(rel_path)
            )
        return baseline

    async def _build_console_snapshots(
        self,
        baseline: dict[str, _ConsoleFileState],
    ) -> list[FileSnapshot]:
        """基于命令前后工作区差异构建可精确回滚的文件快照。"""
        modified, deleted, untracked = await self._git_list_dirty_paths()
        snapshots: list[FileSnapshot] = []
        candidate_paths = sorted(set(baseline) | modified | deleted | untracked)

        for rel_path in candidate_paths:
            before = baseline.get(rel_path)
            current = self._read_console_file_state(
                self._absolute_from_relative(rel_path)
            )

            if before is not None:
                if self._console_state_equal(before, current):
                    continue
                snapshots.append(self._snapshot_from_console_baseline(rel_path, before, current))
                continue

            # 不在 baseline 中，说明命令前它是干净 tracked 文件或根本不存在。
            if rel_path in untracked:
                if current.exists:
                    snapshots.append(FileSnapshot(
                        path=str(self._absolute_from_relative(rel_path)),
                        action="create",
                        original_content=None,
                        original_mode=None,
                    ))
                continue

            if rel_path in modified or rel_path in deleted:
                original_content = await self._read_git_tracked_content(rel_path)
                original_mode = await self._read_git_tracked_mode(rel_path)
                snapshots.append(FileSnapshot(
                    path=str(self._absolute_from_relative(rel_path)),
                    action="delete" if rel_path in deleted else "modify",
                    original_content=original_content,
                    original_mode=original_mode,
                ))

        return snapshots

    async def _git_list_dirty_paths(self) -> tuple[set[str], set[str], set[str]]:
        """列出 git 工作区中的 modified / deleted / untracked 路径。"""
        modified = set(await self._run_git_path_list("ls-files", "-m", "-z"))
        deleted = set(await self._run_git_path_list("ls-files", "-d", "-z"))
        untracked = set(await self._run_git_path_list(
            "ls-files", "-o", "--exclude-standard", "-z"
        ))
        return modified, deleted, untracked

    async def _run_git_path_list(self, *args: str) -> list[str]:
        """执行 git 命令并解析 NUL 分隔的相对路径列表。"""
        import asyncio

        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._working_dir,
            **_hidden_subprocess_kwargs(),
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        if process.returncode != 0 or not stdout:
            return []
        return [
            item
            for item in stdout.decode("utf-8", errors="replace").split("\x00")
            if item
        ]

    async def _read_git_tracked_content(self, rel_path: str) -> bytes | None:
        """读取 git index 中的 tracked 文件内容。"""
        import asyncio

        process = await asyncio.create_subprocess_exec(
            "git",
            "show",
            f":{rel_path}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._working_dir,
            **_hidden_subprocess_kwargs(),
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        if process.returncode != 0:
            return None
        return stdout

    async def _read_git_tracked_mode(self, rel_path: str) -> int | None:
        """读取 git index 中的 tracked 文件 mode。"""
        import asyncio

        process = await asyncio.create_subprocess_exec(
            "git",
            "ls-files",
            "--stage",
            "--",
            rel_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._working_dir,
            **_hidden_subprocess_kwargs(),
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        if process.returncode != 0 or not stdout:
            return None
        line = stdout.decode("utf-8", errors="replace").splitlines()[0].strip()
        if not line:
            return None
        try:
            mode_text = line.split()[0]
            return int(mode_text, 8)
        except (ValueError, IndexError):
            return None

    def _read_console_file_state(self, path: Path) -> _ConsoleFileState:
        """读取当前文件状态。"""
        try:
            exists = path.exists()
        except OSError:
            exists = False

        if not exists:
            return _ConsoleFileState(
                path=str(path),
                exists=False,
                content=None,
                mode=None,
            )

        try:
            content = path.read_bytes()
        except OSError:
            content = None
        try:
            mode = path.stat().st_mode
        except OSError:
            mode = None

        return _ConsoleFileState(
            path=str(path),
            exists=True,
            content=content,
            mode=mode,
        )

    def _absolute_from_relative(self, rel_path: str) -> Path:
        """将 git 相对路径转换为工作目录内绝对路径。"""
        return (Path(self._working_dir) / Path(rel_path)).resolve()

    @staticmethod
    def _console_state_equal(
        before: _ConsoleFileState,
        current: _ConsoleFileState,
    ) -> bool:
        """判断命令前后的文件状态是否一致。"""
        return (
            before.exists == current.exists
            and before.content == current.content
            and before.mode == current.mode
        )

    def _snapshot_from_console_baseline(
        self,
        rel_path: str,
        before: _ConsoleFileState,
        current: _ConsoleFileState,
    ) -> FileSnapshot:
        """根据命令前状态生成该路径的可回滚快照。"""
        abs_path = str(self._absolute_from_relative(rel_path))
        if not before.exists:
            return FileSnapshot(
                path=abs_path,
                action="create",
                original_content=None,
                original_mode=None,
            )
        if not current.exists:
            return FileSnapshot(
                path=abs_path,
                action="delete",
                original_content=before.content,
                original_mode=before.mode,
            )
        return FileSnapshot(
            path=abs_path,
            action="modify",
            original_content=before.content,
            original_mode=before.mode,
        )
