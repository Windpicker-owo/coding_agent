"""终端偏好与命令启动辅助函数。"""

from __future__ import annotations

import locale
import os
import shutil
import sys
from pathlib import Path


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
        executable = _first_existing(
            ["pwsh", "pwsh.exe"],
            skip_windows_apps=sys.platform == "win32",
        )
        if executable:
            return (
                [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
                "utf-8",
            )
        if sys.platform == "win32":
            fallback = _first_existing(["powershell", "powershell.exe"])
            if fallback:
                return (
                    [fallback, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
                    locale.getpreferredencoding(False) or "utf-8",
                )
        return None

    if normalized in {"powershell", "powershell.exe"}:
        executable = _first_existing(
            ["powershell", "powershell.exe"],
            skip_windows_apps=sys.platform == "win32",
        )
        if executable:
            return (
                [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
                locale.getpreferredencoding(False) or "utf-8",
            )
        return None

    if normalized in {"cmd", "cmd.exe"}:
        executable = os.environ.get("COMSPEC") or _first_existing(["cmd", "cmd.exe"])
        if executable:
            return ([executable, "/d", "/s", "/c", command], locale.getpreferredencoding(False) or "utf-8")
        return None

    if normalized in {"bash", "zsh", "sh"}:
        executable = _first_existing(
            [normalized],
            skip_windows_apps=sys.platform == "win32",
        )
        if executable:
            return ([executable, "-lc", command], "utf-8")
        return None

    return None


def _first_existing(
    candidates: list[str],
    *,
    skip_windows_apps: bool = False,
) -> str | None:
    """返回第一个存在于 PATH 中的候选可执行文件。"""

    for candidate in candidates:
        resolved = shutil.which(candidate)
        if skip_windows_apps and _is_windows_apps_alias(resolved):
            continue
        if resolved:
            return resolved
    return None


def _is_windows_apps_alias(path: str | None) -> bool:
    """判断解析结果是否落在 WindowsApps 别名目录。"""

    if sys.platform != "win32" or not path:
        return False

    try:
        normalized = str(Path(path)).casefold()
    except OSError:
        normalized = str(path).casefold()
    return "\\windowsapps\\" in normalized
