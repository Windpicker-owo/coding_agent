"""Coder Agent retry and fallback helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


def build_retry_profiles(requested_profile: str) -> list[str]:
    """Build retry order: requested profile first, then default coding_coder."""
    normalized = requested_profile.strip()
    profiles: list[str] = []
    if normalized:
        profiles.append(normalized)
    if not profiles or profiles[-1] != "":
        profiles.append("")
    return profiles


def describe_profile(profile_name: str) -> str:
    """Human-readable label for retry logs and status messages."""
    if profile_name:
        return f"配置模型 `{profile_name}`"
    return "默认编码模型"


async def execute_with_retry(
    *,
    executor: Callable[[str], Awaitable[tuple[bool, str]]],
    requested_profile: str,
    notify: Callable[[str], Awaitable[None]],
    max_attempts: int = 3,
    backoff_seconds: tuple[float, ...] = (1.0, 2.0, 4.0),
) -> tuple[bool, str]:
    """Execute coder with retry and fallback-to-default behavior."""
    attempt_logs: list[str] = []
    profiles = build_retry_profiles(requested_profile)

    for profile_index, profile_name in enumerate(profiles):
        profile_label = describe_profile(profile_name)
        await notify(f"Coder 准备使用{profile_label}执行计划...")

        for attempt in range(1, max_attempts + 1):
            try:
                success, result = await executor(profile_name)
            except Exception as exc:
                attempt_logs.append(f"{profile_label}第 {attempt} 次执行异常: {exc}")
                if attempt < max_attempts:
                    delay = backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)]
                    await notify(
                        f"{profile_label}执行失败，{delay:.0f} 秒后重试（{attempt}/{max_attempts}）..."
                    )
                    await asyncio.sleep(delay)
                    continue
                break

            if success:
                prefix = ""
                if attempt_logs:
                    prefix = "[Coder 已通过重试恢复]\n" + "\n".join(attempt_logs) + "\n\n"
                return True, prefix + result

            attempt_logs.append(f"{profile_label}返回失败: {result}")
            break

        has_fallback = profile_index < len(profiles) - 1
        if has_fallback:
            fallback_label = describe_profile(profiles[profile_index + 1])
            await notify(f"{profile_label}不可用，正在切换到{fallback_label}继续尝试...")

    if not attempt_logs:
        attempt_logs.append("Coder 未产生可用结果")
    return False, "\n".join(attempt_logs)
