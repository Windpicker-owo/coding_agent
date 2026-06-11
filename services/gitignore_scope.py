"""基于项目根目录 .gitignore 的研究范围过滤。"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path


@dataclass(frozen=True)
class GitIgnoreRule:
    """一条简化后的 .gitignore 规则。"""

    pattern: str
    negated: bool = False
    directory_only: bool = False


class GitIgnoreScope:
    """读取项目根目录 .gitignore，并提供路径过滤能力。"""

    def __init__(self, project_root: str, raw_content: str, rules: list[GitIgnoreRule]) -> None:
        self.project_root = project_root
        self.raw_content = raw_content
        self.rules = rules

    @classmethod
    def load(cls, project_root: str) -> "GitIgnoreScope":
        root = Path(project_root)
        gitignore_path = root / ".gitignore"
        if not gitignore_path.exists():
            return cls(project_root, "", [])

        try:
            raw_content = gitignore_path.read_text(encoding="utf-8")
        except OSError:
            return cls(project_root, "", [])

        rules: list[GitIgnoreRule] = []
        for raw_line in raw_content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            negated = line.startswith("!")
            if negated:
                line = line[1:].strip()
            if not line:
                continue

            line = line.replace("\\", "/")
            directory_only = line.endswith("/")
            normalized = line.strip("/")
            if not normalized:
                continue
            rules.append(GitIgnoreRule(normalized, negated=negated, directory_only=directory_only))

        return cls(project_root, raw_content, rules)

    def is_ignored(self, path: str) -> bool:
        """判断相对路径是否被 .gitignore 忽略。"""
        normalized = path.replace("\\", "/").strip("/")
        if not normalized:
            return False

        ignored = False
        for rule in self.rules:
            if self._matches(rule, normalized):
                ignored = not rule.negated
        return ignored

    def filter_paths(self, paths: list[str]) -> tuple[list[str], list[str]]:
        """过滤一组相对路径，返回 (保留, 忽略)。"""
        kept: list[str] = []
        ignored: list[str] = []
        for path in paths:
            if self.is_ignored(path):
                ignored.append(path)
            else:
                kept.append(path)
        return kept, ignored

    def _matches(self, rule: GitIgnoreRule, path: str) -> bool:
        pattern = rule.pattern
        if rule.directory_only and (path == pattern or path.startswith(pattern + "/")):
            return True

        if "/" not in pattern:
            parts = path.split("/")
            return any(fnmatch(part, pattern) for part in parts)

        return fnmatch(path, pattern) or path.startswith(pattern + "/")