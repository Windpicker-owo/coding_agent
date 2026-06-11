"""Bash 命令审批规则引擎。"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path


class ApprovalDecision(str, Enum):
    """用户审批决策。"""
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"
    ALLOW_FOREVER = "allow_forever"
    DENY = "deny"


class PermissionCheckResult(str, Enum):
    """权限检查结果。"""
    ALLOWED = "allowed"
    DENIED = "denied"
    NEEDS_APPROVAL = "needs_approval"


class PermissionManager:
    """Bash 命令审批规则引擎。

    规则优先级：永久拒绝 > 永久允许 > 会话允许 > 需审批
    """

    def __init__(self, project_root: str) -> None:
        self._project_root = project_root
        self._rules_path = Path(project_root) / ".agents" / "permissions.json"
        self._forever_rules: dict = self._load_rules()
        self._session_rules: dict[str, list[str]] = {}  # session_id -> allowed prefixes

    def check_permission(self, session_id: str, command: str) -> PermissionCheckResult:
        """检查命令权限。"""
        tokens = command.strip().split()
        if not tokens:
            return PermissionCheckResult.NEEDS_APPROVAL

        # 1. 永久拒绝
        for prefix in self._forever_rules.get("denied", []):
            if self._prefix_match(tokens, prefix):
                return PermissionCheckResult.DENIED

        # 2. 永久允许
        for prefix in self._forever_rules.get("allowed", []):
            if self._prefix_match(tokens, prefix):
                return PermissionCheckResult.ALLOWED

        # 3. 会话允许
        session_prefixes = self._session_rules.get(session_id, [])
        for prefix in session_prefixes:
            if self._prefix_match(tokens, prefix):
                return PermissionCheckResult.ALLOWED

        # 4. 需审批
        return PermissionCheckResult.NEEDS_APPROVAL

    def add_session_rule(self, session_id: str, prefix: str) -> None:
        """添加会话级允许前缀。"""
        self._session_rules.setdefault(session_id, []).append(prefix)

    def add_forever_rule(self, prefix: str, allow: bool) -> None:
        """添加永久规则并持久化。"""
        key = "allowed" if allow else "denied"
        if prefix not in self._forever_rules[key]:
            self._forever_rules[key].append(prefix)
            self._save_rules()

    def clear_session_rules(self, session_id: str) -> None:
        """清除会话级规则。"""
        self._session_rules.pop(session_id, None)

    @staticmethod
    def _prefix_match(command_tokens: list[str], prefix: str) -> bool:
        """前缀匹配：将 prefix 分割为 tokens，逐 token 完全匹配。

        "git" 匹配 "git status", "git diff"
        "uv run pytest" 匹配 "uv run pytest -v"
        "git" 不匹配 "github-cli"（因为 token 完全匹配）
        """
        prefix_tokens = prefix.strip().split()
        if not prefix_tokens or len(prefix_tokens) > len(command_tokens):
            return False
        return all(ct == pt for ct, pt in zip(command_tokens, prefix_tokens))

    def _load_rules(self) -> dict:
        """从 .agents/permissions.json 加载规则。"""
        if not self._rules_path.exists():
            return {"allowed": [], "denied": []}
        try:
            data = json.loads(self._rules_path.read_text(encoding="utf-8"))
            return {
                "allowed": data.get("allowed", []),
                "denied": data.get("denied", []),
            }
        except (json.JSONDecodeError, OSError):
            return {"allowed": [], "denied": []}

    def _save_rules(self) -> None:
        """保存规则到 .agents/permissions.json。"""
        try:
            self._rules_path.parent.mkdir(parents=True, exist_ok=True)
            self._rules_path.write_text(
                json.dumps(self._forever_rules, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass
