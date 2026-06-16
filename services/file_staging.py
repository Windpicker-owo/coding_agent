"""文件变更追踪暂存区。

Coder Agent 的 write/edit 会先登记原始内容，再立刻 materialize 到真实磁盘，
保证 console/read/edit/write 看到的是同一份工作区视图。

暂存区的职责变为：
1. 记录每个被 write/edit 触碰文件的初始内容
2. 在成功结束时根据“初始内容 vs 当前磁盘内容”生成总体 diff
3. 在失败时把已追踪文件恢复回初始内容
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from src.kernel.logger import get_logger
from .gitignore_scope import GitIgnoreScope

logger = get_logger("coding_agent.file_staging")

_MAX_TRACKED_FILE_BYTES = 2 * 1024 * 1024
_DEFAULT_SKIPPED_DIRS = {
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    ".next",
    ".turbo",
    ".cache",
}


@dataclass
class StagedChange:
    """单个暂存变更。"""
    path: str           # 绝对路径
    action: Literal["create", "modify"]
    new_content: str    # 新内容
    original_content: str | None  # 原始内容（modify 时为磁盘上的原始内容，create 时为 None）
    original_mode: int | None = None


@dataclass
class CommitResult:
    """提交结果。"""
    files_changed: list[str] = field(default_factory=list)
    diffs: dict[str, str] = field(default_factory=dict)  # path -> unified diff
    summary: str = ""
    combined_diff: str = ""


@dataclass
class WorkspaceSnapshot:
    """工作区基线中的单个文件快照。"""
    path: str
    display_path: str
    content: str
    mode: int | None


@dataclass
class WorkspaceFileState:
    """当前工作区扫描得到的文件状态。"""
    path: str
    display_path: str
    content: str
    mode: int | None


class FileStagingArea:
    """文件变更追踪暂存区。

    生命周期：
    1. CoderAgent.execute() 开始时创建并挂到 session.staging_area
    2. write/edit 工具检测到暂存区存在时先登记原始内容，再直接写入磁盘
    3. read/console 等工具始终读取同一份真实磁盘内容
    4. Coder 成功 → commit() 基于初始内容与当前磁盘状态生成最终 diff
    5. Coder 失败/异常 → rollback() 恢复已追踪文件
    """

    def __init__(
        self,
        working_directory: str,
        linked_directories: list[str] | None = None,
    ) -> None:
        self._working_directory = working_directory
        self._primary_root = Path(working_directory).resolve()
        self._tracked_roots: list[Path] = [self._primary_root]
        for linked in linked_directories or []:
            resolved = Path(linked).resolve()
            if resolved not in self._tracked_roots:
                self._tracked_roots.append(resolved)
        self._scopes = {
            str(root): GitIgnoreScope.load(str(root))
            for root in self._tracked_roots
        }
        self._staged: dict[str, StagedChange] = {}  # path -> StagedChange
        self._explicit_paths: set[str] = set()
        self._expanded_scan_roots: set[str] = set()
        self._baseline = self._capture_workspace_snapshot()

    def stage_write(
        self,
        path: str,
        content: str,
        is_new: bool,
    ) -> Literal["create", "modify"]:
        """登记并 materialize 一次 write 操作。"""
        path = str(Path(path).resolve())
        self.observe_path(path)
        existing = self._staged.get(path)
        previous = existing
        original = existing.original_content if existing else None
        original_mode = existing.original_mode if existing else None
        action: Literal["create", "modify"] = "create" if is_new else "modify"
        if existing and existing.action == "create":
            action = "create"

        if action == "create" and existing is None:
            # 新建文件，original_content 为 None
            original = None
        elif action == "modify" and existing is None:
            # 修改已有文件，但还没暂存过：需要读取磁盘原始内容
            try:
                original = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                original = None
            try:
                original_mode = Path(path).stat().st_mode
            except OSError:
                original_mode = None

        change = StagedChange(
            path=path,
            action=action,
            new_content=content,
            original_content=original,
            original_mode=original_mode,
        )
        self._staged[path] = change
        try:
            self._materialize_change(change)
        except OSError:
            if previous is None:
                self._staged.pop(path, None)
            else:
                self._staged[path] = previous
            raise
        return action

    def stage_edit(
        self,
        path: str,
        original: str,
        new_content: str,
    ) -> Literal["create", "modify"]:
        """登记并 materialize 一次 edit 操作。"""
        path = str(Path(path).resolve())
        self.observe_path(path)
        # 如果已有暂存，保留最初的 original_content
        existing = self._staged.get(path)
        previous = existing
        first_original = existing.original_content if existing else original
        original_mode = existing.original_mode if existing else None
        if original_mode is None:
            try:
                original_mode = Path(path).stat().st_mode
            except OSError:
                original_mode = None

        # 判断 action：如果之前是 create，保持 create
        action: Literal["create", "modify"] = "modify"
        if existing and existing.action == "create":
            action = "create"

        change = StagedChange(
            path=path,
            action=action,
            new_content=new_content,
            original_content=first_original,
            original_mode=original_mode,
        )
        self._staged[path] = change
        try:
            self._materialize_change(change)
        except OSError:
            if previous is None:
                self._staged.pop(path, None)
            else:
                self._staged[path] = previous
            raise
        return action

    def get_staged_content(self, path: str) -> str | None:
        """获取已追踪文件当前内容。优先以磁盘为准，避免与 console 视图分叉。"""
        staged = self._staged.get(path)
        if staged is not None:
            target = Path(path)
            if target.exists():
                try:
                    return target.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
            return None
        return None

    def has_staged(self, path: str) -> bool:
        """检查某文件是否在暂存区中。"""
        return path in self._staged

    def get_staged_action(self, path: str) -> Literal["create", "modify"] | None:
        """获取某个已追踪文件的动作类型。"""
        change = self._staged.get(path)
        return change.action if change is not None else None

    def _display_path(self, path: str) -> str:
        """将绝对路径转换为更适合展示/写入 diff 的路径。"""
        target = Path(path).resolve()
        try:
            return target.relative_to(self._primary_root).as_posix()
        except ValueError:
            pass

        for root in self._tracked_roots[1:]:
            try:
                relative = target.relative_to(root).as_posix()
                return f"[{root.name}]/{relative}"
            except ValueError:
                continue

        return target.as_posix()

    @property
    def staged_paths(self) -> list[str]:
        """返回所有已暂存的文件路径。"""
        return list(self._staged.keys())

    def observe_path(self, path: str) -> None:
        """将显式触达的路径纳入追踪，必要时扩展 ignored 子树扫描。"""
        resolved = str(Path(path).resolve())
        self._explicit_paths.add(resolved)
        target = Path(resolved)

        expanded_root = self._find_ignored_anchor(target)
        if expanded_root is not None:
            self._expand_scan_root(expanded_root)

        if resolved not in self._baseline:
            snapshot = self._capture_file_snapshot(target)
            if snapshot is not None:
                self._baseline[resolved] = snapshot

    def _capture_workspace_snapshot(self) -> dict[str, WorkspaceSnapshot]:
        """捕获 coder 开始时的工作区文本文件快照。"""
        snapshot: dict[str, WorkspaceSnapshot] = {}
        for file_path in self._iter_tracked_files():
            content = self._read_trackable_text(file_path)
            if content is None:
                continue
            try:
                mode = file_path.stat().st_mode
            except OSError:
                mode = None
            resolved = str(file_path.resolve())
            snapshot[resolved] = WorkspaceSnapshot(
                path=resolved,
                display_path=self._display_path(resolved),
                content=content,
                mode=mode,
            )
        return snapshot

    def _capture_current_workspace(self) -> dict[str, WorkspaceFileState]:
        """捕获当前工作区文本文件状态。"""
        state: dict[str, WorkspaceFileState] = {}
        for file_path in self._iter_tracked_files():
            file_state = self._capture_file_state(file_path)
            if file_state is None:
                continue
            state[file_state.path] = file_state

        for root_path in sorted(self._expanded_scan_roots):
            for file_path in self._iter_root_files(Path(root_path), scope=None):
                file_state = self._capture_file_state(file_path)
                if file_state is None:
                    continue
                state[file_state.path] = file_state

        for explicit_path in sorted(self._explicit_paths):
            if explicit_path in state:
                continue
            file_state = self._capture_file_state(Path(explicit_path))
            if file_state is not None:
                state[file_state.path] = file_state
        return state

    def _iter_tracked_files(self) -> list[Path]:
        """遍历工作区内应纳入 diff/回滚的文本文件。"""
        files: list[Path] = []
        for root in self._tracked_roots:
            files.extend(self._iter_root_files(root, scope=self._scopes[str(root)]))
        return files

    def _iter_root_files(
        self,
        root: Path,
        *,
        scope: GitIgnoreScope | None,
    ) -> list[Path]:
        """遍历指定根目录下的候选文件。"""
        files: list[Path] = []
        if not root.exists() or not root.is_dir():
            return files

        for dirpath, dirnames, filenames in os.walk(root):
            current_dir = Path(dirpath)
            kept_dirs: list[str] = []
            for dirname in dirnames:
                if dirname in _DEFAULT_SKIPPED_DIRS:
                    continue
                child = current_dir / dirname
                if scope is not None:
                    rel_path = child.relative_to(root).as_posix()
                    if scope.is_ignored(rel_path):
                        continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs

            for filename in filenames:
                candidate = current_dir / filename
                if candidate.is_symlink():
                    continue
                if scope is not None:
                    rel_path = candidate.relative_to(root).as_posix()
                    if scope.is_ignored(rel_path):
                        continue
                files.append(candidate)
        return files

    def _capture_file_snapshot(self, target: Path) -> WorkspaceSnapshot | None:
        """捕获单个文件的基线快照。"""
        content = self._read_trackable_text(target)
        if content is None:
            return None
        try:
            mode = target.stat().st_mode
        except OSError:
            mode = None
        resolved = str(target.resolve())
        return WorkspaceSnapshot(
            path=resolved,
            display_path=self._display_path(resolved),
            content=content,
            mode=mode,
        )

    def _capture_file_state(self, target: Path) -> WorkspaceFileState | None:
        """捕获单个文件当前状态。"""
        content = self._read_trackable_text(target)
        if content is None:
            return None
        try:
            mode = target.stat().st_mode
        except OSError:
            mode = None
        resolved = str(target.resolve())
        return WorkspaceFileState(
            path=resolved,
            display_path=self._display_path(resolved),
            content=content,
            mode=mode,
        )

    def _locate_tracked_root(
        self,
        target: Path,
    ) -> tuple[Path, GitIgnoreScope, str] | None:
        """定位路径所属的 tracked root 及其相对路径。"""
        resolved = target.resolve()
        for root in self._tracked_roots:
            try:
                relative = resolved.relative_to(root).as_posix()
            except ValueError:
                continue
            return root, self._scopes[str(root)], relative
        return None

    def _find_ignored_anchor(self, target: Path) -> Path | None:
        """查找应扩展扫描的 ignored 子树根目录。"""
        location = self._locate_tracked_root(target)
        if location is None:
            return None
        root, scope, relative = location
        if not relative or not scope.is_ignored(relative):
            return None

        focus = target if target.is_dir() else target.parent
        try:
            relative_focus = focus.resolve().relative_to(root)
        except ValueError:
            return None

        parts = relative_focus.parts
        if not parts:
            return None

        for depth in range(1, len(parts) + 1):
            candidate_rel = Path(*parts[:depth]).as_posix()
            if scope.is_ignored(candidate_rel):
                return root / Path(*parts[:depth])
        return None

    def _expand_scan_root(self, root: Path) -> None:
        """将 ignored 子树加入后续扫描，并补采其基线内容。"""
        resolved = str(root.resolve())
        if resolved in self._expanded_scan_roots:
            return
        self._expanded_scan_roots.add(resolved)
        for file_path in self._iter_root_files(Path(resolved), scope=None):
            snapshot = self._capture_file_snapshot(file_path)
            if snapshot is None:
                continue
            self._baseline.setdefault(snapshot.path, snapshot)

    @staticmethod
    def _read_trackable_text(target: Path) -> str | None:
        """读取可追踪的文本文件；二进制或过大文件返回 None。"""
        try:
            raw = target.read_bytes()
        except OSError:
            return None

        if len(raw) > _MAX_TRACKED_FILE_BYTES:
            return None
        if b"\x00" in raw[:1024]:
            return None
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _read_text_if_exists(target: Path) -> str | None:
        """读取磁盘文件，不存在时返回 None。"""
        if not target.exists() or not target.is_file():
            return None
        return target.read_text(encoding="utf-8", errors="replace")

    @staticmethod
    def _build_diff(
        display_path: str,
        original_content: str | None,
        final_content: str | None,
    ) -> str:
        """为单个文件构建最终 unified diff。"""
        if original_content is None and final_content is None:
            return "(无差异)"

        if final_content is None:
            original_lines = (original_content or "").splitlines(keepends=True)
            diff_lines = list(difflib.unified_diff(
                original_lines,
                [],
                fromfile=f"a/{display_path}",
                tofile="/dev/null",
                lineterm="\n",
            ))
            return "".join(diff_lines) if diff_lines else "(无差异)"

        final_lines = final_content.splitlines(keepends=True)
        if original_content is None:
            diff_lines = list(difflib.unified_diff(
                [],
                final_lines,
                fromfile="/dev/null",
                tofile=f"b/{display_path}",
                lineterm="\n",
            ))
            return "".join(diff_lines) if diff_lines else "(空文件)"

        original_lines = original_content.splitlines(keepends=True)
        diff_lines = list(difflib.unified_diff(
            original_lines,
            final_lines,
            fromfile=f"a/{display_path}",
            tofile=f"b/{display_path}",
            lineterm="\n",
        ))
        return "".join(diff_lines) if diff_lines else "(无差异)"

    @staticmethod
    def _materialize_change(change: StagedChange) -> None:
        """把追踪到的最新内容落到真实磁盘。"""
        target = Path(change.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(change.new_content, encoding="utf-8", newline="")

    async def commit(self) -> CommitResult:
        """根据 coder 开始时的工作区基线与当前磁盘状态生成提交结果。"""
        result = CommitResult()
        create_paths: list[str] = []
        modify_paths: list[str] = []
        delete_paths: list[str] = []
        current_workspace = self._capture_current_workspace()
        all_paths = sorted(
            set(self._baseline.keys())
            | set(current_workspace.keys())
            | set(self._explicit_paths)
        )

        for path in all_paths:
            try:
                baseline = self._baseline.get(path)
                staged = self._staged.get(path)
                if baseline is None and staged is not None and staged.original_content is not None:
                    baseline = WorkspaceSnapshot(
                        path=path,
                        display_path=self._display_path(path),
                        content=staged.original_content,
                        mode=staged.original_mode,
                    )
                current = current_workspace.get(path)
                original_content = baseline.content if baseline is not None else None
                final_content = current.content if current is not None else None
                if final_content == original_content:
                    continue

                display_path = (
                    baseline.display_path
                    if baseline is not None
                    else (current.display_path if current is not None else self._display_path(path))
                )

                result.files_changed.append(display_path)
                if baseline is None and current is not None:
                    create_paths.append(display_path)
                elif baseline is not None and current is None:
                    delete_paths.append(display_path)
                else:
                    modify_paths.append(display_path)

                diff_text = self._build_diff(
                    display_path,
                    original_content,
                    final_content,
                )
                result.diffs[display_path] = diff_text

            except OSError as e:
                logger.error(f"暂存区 commit 写入 {path} 失败: {e}")

        # 生成摘要
        parts: list[str] = []
        if result.files_changed:
            parts.append(f"共 {len(result.files_changed)} 个文件发生变更")
        if create_paths:
            parts.append("新建文件: " + ", ".join(create_paths))
        if modify_paths:
            parts.append("修改文件: " + ", ".join(modify_paths))
        if delete_paths:
            parts.append("删除文件: " + ", ".join(delete_paths))
        result.summary = "\n".join(parts) if parts else "无变更"
        result.combined_diff = "\n\n".join(
            result.diffs[path]
            for path in result.files_changed
            if result.diffs.get(path)
        )

        # 清空暂存区
        self._staged.clear()
        self._explicit_paths.clear()
        self._expanded_scan_roots.clear()

        return result

    def rollback(self) -> None:
        """恢复工作区到 coder 开始时的基线状态。"""
        current_workspace = self._capture_current_workspace()
        all_paths = sorted(
            set(self._baseline.keys())
            | set(current_workspace.keys())
            | set(self._explicit_paths)
        )
        restored = 0

        for path in all_paths:
            baseline = self._baseline.get(path)
            staged = self._staged.get(path)
            if baseline is None and staged is not None and staged.original_content is not None:
                baseline = WorkspaceSnapshot(
                    path=path,
                    display_path=self._display_path(path),
                    content=staged.original_content,
                    mode=staged.original_mode,
                )
            current = current_workspace.get(path)
            original_content = baseline.content if baseline is not None else None
            final_content = current.content if current is not None else None
            if final_content == original_content:
                continue

            target = Path(path)
            try:
                if baseline is None:
                    if target.exists() and target.is_file():
                        target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(
                        baseline.content,
                        encoding="utf-8",
                        newline="",
                    )
                    if baseline.mode is not None:
                        try:
                            os.chmod(target, baseline.mode)
                        except OSError:
                            pass
                restored += 1
            except OSError as e:
                logger.warning(f"暂存区回滚 {path} 失败: {e}")
        self._staged.clear()
        self._explicit_paths.clear()
        self._expanded_scan_roots.clear()
        logger.info(f"暂存区已回滚，恢复 {restored} 个工作区变更")
