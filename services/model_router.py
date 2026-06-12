"""Coder Agent 模型路由服务。

Main Agent 通过 model_profile 参数指定 Coder 使用的模型 Profile，
ModelRouter 负责校验 Profile 名称并构建对应的 ModelSet。
"""

from __future__ import annotations

from src.kernel.llm import ModelSet

from ..config import CoderModelProfile


class ModelRouter:
    """Coder Agent 模型路由器。

    根据 Main Agent 传入的 profile_name 查找对应的 CoderModelProfile，
    校验有效性后调用 get_model_set_by_name() 构建 ModelSet。
    """

    def __init__(self, profiles: list[CoderModelProfile]) -> None:
        """构建 profile 名称到 profile 的查找字典。

        Args:
            profiles: 配置中 model_profiles 列表
        """
        self._profiles: dict[str, CoderModelProfile] = {}
        for p in profiles:
            if p.profile_name:
                self._profiles[p.profile_name] = p

    def get_profile(self, name: str) -> CoderModelProfile:
        """获取指定名称的 Profile。

        Args:
            name: Profile 名称

        Returns:
            对应的 CoderModelProfile

        Raises:
            ValueError: 指定名称的 Profile 不存在
        """
        if name not in self._profiles:
            available = ", ".join(sorted(self._profiles.keys())) if self._profiles else "(无)"
            raise ValueError(
                f"模型 profile '{name}' 不存在。可用: [{available}]"
            )
        return self._profiles[name]

    def build_model_set(self, profile: CoderModelProfile) -> ModelSet:
        """根据 Profile 构建 ModelSet。

        Args:
            profile: CoderModelProfile 实例

        Returns:
            构建好的 ModelSet，可直接用于 LLMRequest

        Raises:
            KeyError: model_name 对应的模型在 model.toml 中不存在
        """
        from src.core.config import get_model_config

        config = get_model_config()
        return config.get_model_set_by_name(
            profile.model_name,
            temperature=profile.temperature,
            max_tokens=profile.max_tokens,
        )

    @property
    def has_profiles(self) -> bool:
        """是否有可用的 Profile。"""
        return len(self._profiles) > 0

    def describe_for_prompt(self) -> str:
        """生成供 system prompt 注入的可用模型列表文本。

        Returns:
            <available_coder_models> XML 块，无 profiles 时返回空字符串
        """
        if not self._profiles:
            return ""

        entries: list[str] = []
        for name, profile in self._profiles.items():
            tags_str = ", ".join(profile.tags) if profile.tags else "通用"
            desc = profile.description or "无描述"
            model_name = profile.model_name or "未知"
            entries.append(
                f"- **{name}** (模型: `{model_name}`)\n"
                f"  标签: {tags_str}\n"
                f"  适用场景: {desc}"
            )

        return (
            "<available_coder_models>\n"
            + "你可以使用以下模型来完成计划中的任务，选择时请根据任务需求和模型特点进行匹配，如果可以，尽量将一份计划拆分成多个子任务（例如一份前端一份后端），依次使用最适合的模型实现，来提高整体完成度：\n"
            + "\n".join(entries)
            + "\n</available_coder_models>"
        )
