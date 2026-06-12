"""子 Agent 编排器。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from src.kernel.logger import get_logger

logger = get_logger("coding_agent.orchestration")


@dataclass
class ResearchTarget:
    """研究目标。"""
    module_path: str
    research_focus: str


@dataclass
class ResearchReport:
    """研究报告。"""
    module_path: str
    success: bool
    report: dict | str


@dataclass
class CoderResult:
    """编码结果。"""
    success: bool
    summary: str
    files_changed: list[str]


class CodingOrchestrator:
    """管理 coding agent 的子代理编排。"""

    def __init__(self, plugin: Any, stream_id: str) -> None:
        self._plugin = plugin
        self._stream_id = stream_id

    async def run_project_scout(self, project_root: str, gitignore_content: str = "") -> dict:
        """执行项目侦察。"""
        from .agents.project_scout import ProjectScoutAgent

        scout = ProjectScoutAgent(stream_id=self._stream_id, plugin=self._plugin)
        success, result = await scout.execute(
            project_root=project_root,
            gitignore_content=gitignore_content,
        )

        if success and isinstance(result, dict):
            return result
        elif isinstance(result, str):
            return {"raw": result}
        return {}

    async def run_parallel_research(
        self,
        targets: list[ResearchTarget],
        max_parallel: int = 6,
        gitignore_content: str = "",
        progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> list[ResearchReport]:
        """并行派出多个 ModuleResearcher。"""
        from .agents.module_researcher import ModuleResearcherAgent

        semaphore = asyncio.Semaphore(max_parallel)
        state_lock = asyncio.Lock()
        active_agents: dict[str, dict[str, str]] = {}
        completed = 0

        async def _emit_progress(current_module: str) -> None:
            if progress_callback is None:
                return
            await progress_callback({
                "total": len(targets),
                "completed": completed,
                "current_module": current_module,
                "active_agents": list(active_agents.values()),
            })

        async def _research_one(index: int, target: ResearchTarget) -> ResearchReport:
            nonlocal completed
            worker_name = f"researcher-{index + 1}"
            async with semaphore:
                async with state_lock:
                    active_agents[worker_name] = {
                        "name": worker_name,
                        "module_path": target.module_path,
                        "focus": target.research_focus,
                    }
                    await _emit_progress(target.module_path)
                try:
                    researcher = ModuleResearcherAgent(
                        stream_id=self._stream_id, plugin=self._plugin
                    )
                    success, result = await researcher.execute(
                        module_path=target.module_path,
                        research_focus=target.research_focus,
                        gitignore_content=gitignore_content,
                    )
                    return ResearchReport(
                        module_path=target.module_path,
                        success=success,
                        report=result,
                    )
                except Exception as e:
                    logger.error(f"研究 {target.module_path} 失败: {e}")
                    return ResearchReport(
                        module_path=target.module_path,
                        success=False,
                        report=str(e),
                    )
                finally:
                    async with state_lock:
                        active_agents.pop(worker_name, None)
                        completed += 1
                        await _emit_progress(target.module_path)

        tasks = [_research_one(i, target) for i, target in enumerate(targets)]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        reports: list[ResearchReport] = []
        for i, result in enumerate(results):
            if isinstance(result, ResearchReport):
                reports.append(result)
            elif isinstance(result, Exception):
                reports.append(ResearchReport(
                    module_path=targets[i].module_path,
                    success=False,
                    report=str(result),
                ))

        return reports

    async def run_full_research(
        self,
        project_root: str,
        gitignore_content: str = "",
        progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict:
        """执行完整项目研究流程：scout + 并行模块研究 + 虚拟环境检测。

        封装了 ProjectScoutAgent → ModuleResearcherAgent 的完整研究管线，
        返回聚合后的上下文 dict，结构与 _run_project_research 输出对齐。
        """
        from .services.gitignore_scope import GitIgnoreScope
        from .services.project_context import ProjectContextService

        # 1. 加载 gitignore（若调用方未提供）
        gitignore_scope = GitIgnoreScope.load(project_root)
        if not gitignore_content:
            gitignore_content = gitignore_scope.raw_content

        # 2. 侦察
        scout_result = await self.run_project_scout(
            project_root,
            gitignore_content=gitignore_content,
        )

        # 3. 构建研究目标（过滤 gitignore）
        modules = scout_result.get("modules", [])
        raw_module_paths = [m.get("path", "") for m in modules if m.get("path")]
        _, ignored_modules = gitignore_scope.filter_paths(raw_module_paths)
        targets = [
            ResearchTarget(
                module_path=m.get("path", ""),
                research_focus=m.get("description", "模块结构与接口"),
            )
            for m in modules
            if m.get("path") and not gitignore_scope.is_ignored(m.get("path", ""))
        ]

        # 4. 并行研究
        reports = await self.run_parallel_research(
            targets,
            gitignore_content=gitignore_content,
            progress_callback=progress_callback,
        )

        # 5. 检测虚拟环境
        context_svc = ProjectContextService(self._plugin)
        venv_info = await context_svc.detect_project_virtual_env(project_root)

        # 6. 聚合结果
        result: dict[str, Any] = {
            "scout": scout_result,
            "gitignore": {
                "rules_count": len(gitignore_scope.rules),
                "ignored_modules": ignored_modules,
            },
            "modules": [
                {"path": r.module_path, "report": r.report, "success": r.success}
                for r in reports
            ],
            "virtual_environment": venv_info,
        }
        result["scout"]["virtual_environment"] = venv_info

        return result

    async def run_coder_session(
        self, plan: str
    ) -> CoderResult:
        """创建 CoderAgent 执行落地计划。"""
        from .agents.coder import CoderAgent

        coder = CoderAgent(stream_id=self._stream_id, plugin=self._plugin)
        success, result = await coder.execute(
            implementation_plan=plan,
        )

        summary = result if isinstance(result, str) else str(result)
        return CoderResult(
            success=success,
            summary=summary,
            files_changed=[],
        )
