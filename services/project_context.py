"""项目上下文管理服务。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from src.app.plugin_system.base import BaseService


class ProjectContextService(BaseService):
    """管理 .agents/context/ 的项目理解缓存。"""

    service_name = "project_context"
    service_description = "管理项目理解上下文的持久化缓存"

    async def load_context(self, project_root: str) -> dict | None:
        """加载项目上下文缓存。"""
        ctx_path = Path(project_root) / ".agents" / "context" / "project_overview.json"
        if not ctx_path.exists():
            return None
        try:
            data = json.loads(ctx_path.read_text(encoding="utf-8"))
            return data
        except (json.JSONDecodeError, OSError):
            return None

    async def save_context(self, project_root: str, context: dict) -> None:
        """保存项目上下文。"""
        ctx_dir = Path(project_root) / ".agents" / "context"
        ctx_dir.mkdir(parents=True, exist_ok=True)

        # 写入 project_overview.json
        overview_path = ctx_dir / "project_overview.json"
        overview_path.write_text(
            json.dumps(context, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # 写入 metadata.json
        meta_path = ctx_dir / "metadata.json"
        meta_path.write_text(
            json.dumps({"generated_at": time.time()}, indent=2),
            encoding="utf-8",
        )

    async def is_context_stale(
        self, project_root: str, ttl_hours: int = 24
    ) -> bool:
        """检查缓存是否过期。

        同时检查 project_overview.json（实际数据文件）和 metadata.json（时间戳文件），
        两者都必须存在且未过期才视为缓存有效。
        """
        ctx_path = Path(project_root) / ".agents" / "context"
        overview_path = ctx_path / "project_overview.json"
        meta_path = ctx_path / "metadata.json"

        if not overview_path.exists() or not meta_path.exists():
            return True
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            generated_at = data.get("generated_at", 0)
            age_hours = (time.time() - generated_at) / 3600
            return age_hours > ttl_hours
        except (json.JSONDecodeError, OSError):
            return True

    async def save_module_report(
        self, project_root: str, module_path: str, report: dict
    ) -> None:
        """保存单个模块的研究报告到 .agents/context/module_reports/。"""
        reports_dir = Path(project_root) / ".agents" / "context" / "module_reports"
        reports_dir.mkdir(parents=True, exist_ok=True)

        # 用模块路径生成文件名
        safe_name = module_path.replace("/", "_").replace("\\", "_") + ".json"
        report_path = reports_dir / safe_name
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    async def load_agents_config(self, project_root: str) -> str:
        """读取 .agents/ 下的自定义上下文文件。"""
        agents_dir = Path(project_root) / ".agents"

        # 按优先级查找
        for name in ("AGENTS.md", "CLAUDE.md", "CONTEXT.md", "README.md"):
            path = agents_dir / name
            if path.exists():
                try:
                    return path.read_text(encoding="utf-8")
                except OSError:
                    continue

        # 降级到项目根目录的 README
        readme = Path(project_root) / "README.md"
        if readme.exists():
            try:
                return readme.read_text(encoding="utf-8")
            except OSError:
                pass

        return "（无项目上下文文件）"

    async def detect_project_virtual_env(self, project_root: str) -> str:
        """扫描项目根目录下常见虚拟环境目录，返回路径或"未检测到"。
        
        扫描的目录包括: .venv, venv, env, .conda, __pypackages__
        返回第一个找到的目录的相对路径，如果都不存在则返回"未检测到"。
        """
        common_venv_dirs = [".venv", "venv", "env", ".conda", "__pypackages__"]
        project_path = Path(project_root)
        
        for venv_dir in common_venv_dirs:
            venv_path = project_path / venv_dir
            if venv_path.exists() and venv_path.is_dir():
                # 检查是否真的是虚拟环境（包含pyvenv.cfg或site-packages目录）
                if (venv_path / "pyvenv.cfg").exists() or (venv_path / "site-packages").exists():
                    return venv_dir
                # 对于__pypackages__，检查是否包含Python版本目录
                if venv_dir == "__pypackages__":
                    # 查找任何以数字开头的目录（如3.11）
                    for item in venv_path.iterdir():
                        if item.is_dir() and item.name[0].isdigit():
                            return f"{venv_dir}/{item.name}"
        
        return "未检测到"

    async def format_context_as_text(self, context: dict) -> str:
        """把 context dict 格式化为人/LLM 可读的摘要文本。
        
        用于 user 消息和 system reminder 共用。
        兼容两种 context 结构：
        1. 平铺结构（顶层 project_name / tech_stack / modules 等）
        2. 嵌套结构（顶层 scout + modules 研究列表）
        """
        if not context:
            return "（项目上下文为空）"
        
        # 从嵌套结构中提取 scout（若存在）
        scout = context.get("scout", {}) if isinstance(context.get("scout"), dict) else {}
        
        def _get(key: str, default: Any = None) -> Any:
            """优先从顶层取，降级到 scout 子 dict。"""
            val = context.get(key)
            if val is not None:
                return val
            return scout.get(key, default)
        
        lines: list[str] = []
        
        # 项目基本信息
        project_name = _get("project_name", "未知项目")
        lines.append(f"项目名称: {project_name}")
        
        # 技术栈
        tech_stack = _get("tech_stack", [])
        if tech_stack:
            lines.append(f"技术栈: {', '.join(tech_stack[:10])}")  # 最多显示10个
        
        # 虚拟环境
        virtual_env = context.get("virtual_environment") or scout.get("virtual_environment", "未检测到")
        if virtual_env and virtual_env != "未检测到":
            lines.append(f"虚拟环境: {virtual_env}")
        
        # 构建系统
        build_system = _get("build_system", "")
        if build_system:
            lines.append(f"构建系统: {build_system}")
        
        # 模块数量 — 优先使用嵌套 modules 研究列表，降级到 scout.modules
        nested_modules = context.get("modules", [])
        scout_modules = scout.get("modules", [])
        if nested_modules:
            lines.append(f"模块数量: {len(nested_modules)} (含深度研究)")
        elif scout_modules:
            lines.append(f"模块数量: {len(scout_modules)}")
        
        # 配置文件
        config_files = _get("config_files", [])
        if config_files:
            lines.append(f"配置文件: {len(config_files)} 个")
        
        # 关键文件
        key_files = _get("key_files", [])
        if key_files:
            lines.append(f"关键文件: {len(key_files)} 个")
        
        # 源代码根目录
        source_root = _get("source_root", "")
        if source_root:
            lines.append(f"源代码根目录: {source_root}")
        
        # 摘要 — 优先 scout.summary，降级到顶层 summary
        summary = scout.get("summary") or context.get("summary", "")
        if summary:
            # 限制摘要长度
            if len(summary) > 500:
                summary = summary[:500] + "..."
            lines.append(f"项目摘要: {summary}")
        
        return "\n".join(lines)
