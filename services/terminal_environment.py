"""终端偏好与命令启动辅助函数。"""

from __future__ import annotations

import locale
import os
import shutil


def get_preferred_terminal_from_config(config: object | None) -> str:
    """从插件配置中读取优先终端环境。"""

    console_config = getattr(config, "console", None) if config is not None else None
    preferred = getattr(console_config, "preferred_terminal", "") if console_config is not None else ""
    return str(preferred or "").strip()


def build_terminal_launch(command: str, preferred_terminal: str) -> tuple[list[str], str] | None:
    """根据终端偏好构建子进程启动参数。"""

    normalized = preferred_terminal.strip().lower()
    if not normalized:
        return None

    if normalized in {"pwsh", "pwsh.exe"}:
        executable = _first_existing(["pwsh", "pwsh.exe"])
        if executable:
            return ([executable, "-NoLogo", "-NoProfile", "-Command", command], "utf-8")
        return None

    if normalized in {"powershell", "powershell.exe"}:
        executable = _first_existing(["powershell", "powershell.exe"])
        if executable:
            return (
                [executable, "-NoLogo", "-NoProfile", "-Command", command],
                locale.getpreferredencoding(False) or "utf-8",
            )
        return None

    if normalized in {"cmd", "cmd.exe"}:
        executable = os.environ.get("COMSPEC") or _first_existing(["cmd", "cmd.exe"])
        if executable:
            return ([executable, "/d", "/s", "/c", command], locale.getpreferredencoding(False) or "utf-8")
        return None

    if normalized in {"bash", "zsh", "sh"}:
        executable = _first_existing([normalized])
        if executable:
            return ([executable, "-lc", command], "utf-8")
        return None

    return None


def _first_existing(candidates: list[str]) -> str | None:
    """返回第一个存在于 PATH 中的候选可执行文件。"""

    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None