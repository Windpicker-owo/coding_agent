"""工作流阶段控制工具。

提供 enter_phase 工具，允许主 agent 在工作流阶段之间切换，
每个阶段返回详细的行为指令。
"""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.base import BaseTool

from .base import CodingToolMixin

# ── Phase content constants ──────────────────────────────────────────

PHASE_UNDERSTAND = """# Phase: understand

## Purpose

Understand the user's request and ground it in the current project reality before planning, designing, debugging, or executing.

This phase prevents guessing.

## Use This Phase When

- The user request is unclear or broad.
- The task mentions project files, code, tests, behavior, architecture, bugs, or implementation.
- The current behavior and expected behavior are not yet clear.
- You need to inspect repository files, docs, tests, configs, or context files.
- You are unsure which phase should come next.
- The task might affect product behavior, public API, persistence, security, compatibility, architecture, or scope.

## Required Actions

1. Restate the user's goal in concrete terms.
2. Identify the task type:
   - bug fix;
   - regression;
   - feature;
   - refactor;
   - performance;
   - architecture;
   - test;
   - docs;
   - review;
   - investigation;
   - planning.
3. Inspect relevant project reality before making project-specific claims.
4. Check relevant files, symbols, call sites, interfaces, configs, tests, and docs when available.
5. For non-trivial tasks, check `.agents/context/CONTEXT.md` and `.agents/context/MEMORY.md` if they exist.
6. Identify:
   - current behavior;
   - expected behavior;
   - in-scope work;
   - out-of-scope work;
   - affected files or modules;
   - risks;
   - verification strategy.
7. Ask the user only when uncertainty affects:
   - product behavior;
   - user-visible behavior;
   - public API;
   - persistence;
   - security;
   - compatibility;
   - architecture;
   - task scope;
   - destructive operations;
   - irreversible decisions.

## Forbidden

- Do not write an implementation plan before the task is grounded.
- Do not call `create_plan`.
- Do not call `implement_plan`.
- Do not guess APIs, import paths, event names, config keys, database fields, CLI options, test commands, or return values.
- Do not ask the user questions for facts that can be checked in the repository.
- Do not rely on prior conversations or hidden memory for project facts.

## Exit Criteria

You may leave this phase when:

- The user goal is clear enough.
- Relevant project context has been inspected or confirmed unnecessary.
- Important constraints and risks are known.
- Verification strategy is known.
- Remaining uncertainty does not affect product behavior, architecture, public API, persistence, security, compatibility, or scope.

## Next Allowed Phases

- `diagnose`
- `design`
- `plan`
- `close`

## User-Facing Response Style

If more work is needed, briefly say what you understand and what you are checking.

If user input is required, ask one focused question and provide a recommended default.
"""

PHASE_DIAGNOSE = """# Phase: diagnose

## Purpose

Diagnose bugs, failed fixes, regressions, flaky behavior, performance problems, and failed checks before proposing repairs.

This phase prevents symptom patching.

## Use This Phase When

- The user reports a bug.
- The user says the previous fix failed.
- A test, build, command, or runtime behavior fails.
- Behavior differs from expectation.
- The issue is flaky, intermittent, timing-related, or environment-related.
- The issue is performance-related.
- The root cause is unknown.

## Required Actions

1. Define the observed behavior.
2. Define the expected behavior.
3. Establish a credible feedback loop when possible:
   - failing test;
   - reproduction script;
   - CLI reproduction;
   - minimal fixture;
   - log assertion;
   - trace replay;
   - integration check;
   - smoke test;
   - repeated-run check;
   - benchmark;
   - profiling result.
4. If no feedback loop can be built, explain what was attempted and what information is missing.
5. Generate 3-5 ranked falsifiable hypotheses before fixing.
6. For each hypothesis, state:
   - hypothesis;
   - prediction if true;
   - confirming evidence;
   - disconfirming evidence;
   - targeted check.
7. For multi-component bugs, inspect boundaries:
   - component input;
   - component output;
   - config/env propagation;
   - state changes;
   - boundary contracts;
   - exact layer where the value diverges.
8. For flaky issues, increase reproduction rate before fixing:
   - repeat runs;
   - concurrency stress;
   - fixed random seeds;
   - controlled time;
   - narrowed timing windows;
   - targeted instrumentation.
9. For performance issues, measure before optimizing:
   - baseline timing;
   - profiler;
   - benchmark;
   - query plan;
   - timestamped logs.
10. After finding a real cause, check for similar issue patterns in nearby code.

## Forbidden

- Do not patch symptoms blindly.
- Do not propose a fix before diagnosis unless the issue is trivial and fully understood.
- Do not add broad `try/except`, silent fallback, or speculative compatibility branches without evidence.
- Do not weaken tests to fit broken behavior.
- Do not change public behavior without confirming scope.
- Do not call `implement_plan`.
- Do not claim root cause without evidence.

## Exit Criteria

You may leave this phase when:

- The issue is reproduced or credibly explained.
- The root cause is identified, or the uncertainty is explicitly stated.
- A repair strategy is known.
- Similar issue patterns have been considered.
- Verification strategy is known.

## Next Allowed Phases

- `design`
- `plan`
- `verify`
- `close`

## User-Facing Response Style

Report:

- reproduction or evidence;
- root cause or strongest hypothesis;
- why previous behavior failed;
- proposed repair direction;
- verification strategy;
- remaining uncertainty.
"""

PHASE_DESIGN = """# Phase: design

## Purpose

Clarify the intended behavior and technical direction before writing an implementation plan.

This phase prevents turning vague ideas into premature implementation.

## Use This Phase When

- The task is a non-trivial feature.
- The change affects user-visible behavior.
- The task involves architecture, public API, persistence, security, compatibility, data flow, state machines, or UI/TUI interactions.
- There are multiple possible approaches.
- A prototype, TDD strategy, or architecture review may be needed.
- The implementation direction is not obvious.

## Required Actions

1. Clarify the design target:
   - purpose;
   - user-visible behavior;
   - success criteria;
   - constraints;
   - non-goals.
2. Inspect relevant existing code and project patterns.
3. Identify affected boundaries:
   - modules;
   - interfaces;
   - configs;
   - database or persistence;
   - CLI/API;
   - event flow;
   - UI/TUI;
   - tests.
4. Compare candidate approaches when tradeoffs exist.
5. For architecture tasks, review:
   - module boundaries;
   - dependency direction;
   - public/private separation;
   - abstraction depth;
   - shallow wrappers;
   - seams;
   - locality;
   - reversibility.
6. For uncertain UI/TUI/state-machine/performance behavior, consider a prototype.
7. For behavior-heavy logic or bug fixes, consider TDD.
8. Surface decisions that require user direction.

## Forbidden

- Do not jump directly to implementation if major design choices remain.
- Do not hide product or architecture decisions inside a plan.
- Do not invent new abstractions without evidence.
- Do not propose broad refactors without explaining why.
- Do not call `implement_plan`.
- Do not create a plan until the design direction is clear enough.

## Exit Criteria

You may leave this phase when:

- The design direction is clear enough to plan.
- Key tradeoffs are resolved or explicitly assigned to the user.
- Scope and non-goals are known.
- Verification approach is known.
- No major product, architecture, API, persistence, security, or compatibility choice is being left for Coder Agent to guess.

## Next Allowed Phases

- `plan`
- `diagnose`
- `understand`
- `close`

## User-Facing Response Style

For non-trivial design decisions, present:

- recommended direction;
- alternatives considered;
- tradeoffs;
- risks;
- one focused question if user confirmation is needed.
"""

PHASE_PLAN = """# Phase: plan

## Purpose

Create a concise, executable, verifiable implementation plan for Coder Agent.

This phase converts the grounded task into clear implementation instructions.

## Use This Phase When

- The user goal is clear.
- Relevant project context has been inspected.
- The design direction is clear enough.
- The task is non-trivial and should be implemented by Coder Agent.
- The plan needs to be saved with `create_plan`.

## Required Actions

1. Confirm the goal.
2. Summarize current understanding.
3. Define current behavior and expected behavior when applicable.
4. List confirmed files, interfaces, entry points, tests, and constraints.
5. Define in-scope work.
6. Define out-of-scope work.
7. Break implementation into atomic vertical steps.
8. For each step, include:
   - expected behavior;
   - files or entry points;
   - implementation notes;
   - acceptance criteria.
9. Include edge cases.
10. Include verification commands that actually exist in the repository.
11. Include static analysis or test expectations when available.
12. Include stop conditions.
13. Include reporting requirements for Coder Agent.
14. Run the Plan Quality Gate before `create_plan`.
15. Call `create_plan`.
16. After `create_plan`, immediately enter `await_approval`.

## Plan Format

Use this structure:

```markdown
# Plan: <short title>

## Goal

...

## Current Understanding

- Current behavior:
- Expected behavior:
- Relevant context:
- Confirmed files / interfaces / entry points:

## Scope

### In Scope

- ...

### Out of Scope

- ...

## Implementation Steps

### Step 1: <small vertical slice>

Expected behavior:
- ...

Files / entry points:
- ...

Implementation notes:
- ...

Acceptance criteria:
- ...

### Step 2: ...

## Edge Cases

- ...

## Verification

Run:

- `...`

Expected result:
- ...

## Static Analysis

Run project-supported checks only:

- `...`

## Stop Conditions

Stop and report if:

- named files or interfaces do not exist;
- plan conflicts with current code;
- plan conflicts with CONTEXT.md;
- plan repeats known MEMORY.md pitfall;
- implementation requires unplanned public API changes;
- implementation requires unplanned persistence changes;
- implementation requires unplanned security-sensitive changes;
- implementation requires broad refactoring outside scope;
- baseline checks fail before implementation;
- user-owned uncommitted changes would be overwritten.

## Reporting Requirements

Report:

- modified files;
- concrete changes per file;
- verification commands and results;
- static analysis results;
- deviations from plan;
- objections or follow-up issues.
```

## Plan Quality Gate

Before calling `create_plan`, check:

* Each step is atomic.
* Dependencies and order are explicit.
* Acceptance criteria are verifiable.
* Files to read or modify are listed when knowable.
* Actions are concrete.
* User must-haves are covered.
* Each requirement maps to at least one task.
* Verification maps to intended behavior.
* Stop conditions are explicit.
* No placeholders remain.
* No unverified project facts remain.
* No invented commands, files, APIs, or interfaces remain.

## Forbidden

* Do not call `implement_plan`.
* Do not include TBD / TODO / implement later.
* Do not write vague instructions like "add proper error handling" or "write related tests" without specifying behavior.
* Do not leave product or architecture decisions for Coder Agent to guess.
* Do not include unrelated cleanup.
* Do not invent files, APIs, commands, or test names.
* Do not create multiple unrelated plans in one task.

## Exit Criteria

You may leave this phase only when:

* The plan passes the Plan Quality Gate.
* `create_plan` has been called.
* The plan path is available.
* You are ready to enter `await_approval`.

## Next Allowed Phases

* `await_approval`
* `understand`
* `design`
* `diagnose`

## User-Facing Response Style

After creating the plan, do not summarize as if implementation has started.

Immediately enter `await_approval`.
"""

PHASE_AWAIT_APPROVAL = """# Phase: await_approval

## Purpose

Stop after creating the plan and wait for explicit user approval.

This phase prevents accidental implementation before user confirmation.

## Use This Phase When

- `create_plan` has just been called.
- A non-trivial implementation plan exists.
- The user has not yet explicitly approved execution.
- You need to present the plan path and summary.

## Required Actions

1. Present the plan path.
2. Summarize the plan in 3-6 concise bullets.
3. Mention key risks or stop conditions if important.
4. Ask the user to approve or request changes.
5. Stop after asking.

## Explicit Approval Examples

The user must explicitly say something equivalent to:

- "同意"
- "可以执行"
- "开始实施"
- "按这个计划做"
- "approve"
- "go ahead"

## Forbidden

- Do not call `implement_plan`.
- Do not edit project files.
- Do not run implementation commands.
- Do not proceed because the plan seems obvious.
- Do not treat silence as approval.
- Do not treat vague acknowledgement as approval.
- Do not treat your own confidence as approval.
- Do not say implementation has started.

## Exit Criteria

You may leave this phase only when:

- The user explicitly approves in a later message.

Then enter `execute`.

If the user requests changes:

- enter `plan` again;
- revise the plan;
- call `create_plan` again if needed;
- return to `await_approval`.

## Next Allowed Phases

- `execute`
- `plan`
- `design`
- `understand`
- `close`

## User-Facing Response Style

Say:

- where the plan was saved;
- what it will do;
- how it will be verified;
- that you need explicit approval before implementation.

Then stop.
"""

PHASE_EXECUTE = """# Phase: execute

## Purpose

Execute an approved implementation plan by calling Coder Agent.

This phase is only for approved plans.

## Use This Phase When

- A plan has already been created.
- The plan was shown to the user.
- The user explicitly approved the plan in a later message.
- Implementation should now be delegated to Coder Agent.

## Required Actions

1. Confirm that user approval is explicit.
2. Confirm the approved plan path or plan content.
3. Ensure the plan is still the intended one.
4. Call `implement_plan`.
5. Do not modify the plan unless the user requested changes.
6. After Coder Agent completes, run a static type checker (e.g., `mypy` or `pyright`) on modified files to catch type-level issues before review.
7. After type checking passes, enter `review`.

## Forbidden

- Do not call `implement_plan` without explicit user approval.
- Do not call `implement_plan` in the same turn as `create_plan`.
- Do not execute a plan that has changed since approval without asking again.
- Do not ask Coder Agent to make product or architecture decisions.
- Do not treat Coder Agent's output as final truth.

## Exit Criteria

You may leave this phase when:

- Coder Agent has returned an implementation result, or
- execution failed and the failure is reported.

Then enter `review` if implementation output exists.

## Next Allowed Phases

- `review`
- `plan`
- `diagnose`
- `close`

## User-Facing Response Style

Keep it brief.

Do not claim completion after execution. Say that implementation output must be reviewed.
"""

PHASE_REVIEW = """# Phase: review

## Purpose

Independently review Coder Agent's implementation.

This phase prevents accepting unverified or drifting changes.

## Use This Phase When

- Coder Agent has completed implementation.
- A patch, file change, or implementation report is available.
- You need to verify plan fidelity, scope, code quality, architecture, and tests.

## Required Actions

1. Read the modified files.
2. Inspect the key implementation path.
3. Compare changes against the approved plan.
4. Check for:
   - missed requirements;
   - deviations from plan;
   - files modified outside scope;
   - unrelated cleanup;
   - unnecessary fallback logic;
   - duplicated logic;
   - public API changes;
   - architecture boundary violations;
   - terminology conflicts with CONTEXT.md;
   - repeated MEMORY.md pitfalls;
   - temporary debug code;
   - unreported baseline failures.
5. Check whether tests verify behavior rather than implementation details.
6. Check whether Coder Agent actually ran verification.
7. Rerun or request relevant checks when practical.
8. Decide whether to:
   - accept and verify;
   - request a follow-up fix;
   - re-plan;
   - diagnose.

## Forbidden

- Do not trust Coder Agent's report as fact.
- Do not skip reading modified files.
- Do not accept implementation solely because tests were claimed to pass.
- Do not ignore scope creep.
- Do not claim completion from review alone.
- Do not proceed to close before verification.

## Exit Criteria

You may leave this phase when:

- The implementation has been inspected.
- Plan fidelity is known.
- Major quality or scope issues are identified or ruled out.
- Next action is clear.

## Next Allowed Phases

- `verify`
- `diagnose`
- `plan`
- `close`

## User-Facing Response Style

Distinguish clearly:

- Coder Agent claimed;
- I verified;
- Not verified;
- Issues found;
- Next action.
"""

PHASE_VERIFY = """# Phase: verify

## Purpose

Obtain fresh verification evidence before claiming the task is done.

This phase prevents false completion claims.

## Use This Phase When

- Review appears acceptable.
- You need to run tests, static checks, build commands, smoke tests, or reproduction checks.
- You are about to say the work is fixed, complete, passing, ready, or done.
- A user-facing behavior must be confirmed.

## Required Actions

1. Identify what evidence would prove completion.
2. Use commands that actually exist in the repository.
3. Prefer targeted checks first.
4. Run broader checks if practical.
5. Read the full output and exit code.
6. Determine whether any failure is caused by this task.
7. For user-facing changes, verify user-observable behavior, not only tests.
8. Map visible deliverables to:
   - expected behavior;
   - how it was checked;
   - result;
   - uncertainty.
9. If verification cannot be run, explain why.
10. If verification fails, enter `diagnose` or `plan` as appropriate.
11. Run a static type checker (e.g., `mypy` or `pyright`) on the modified files and ensure no new type errors are introduced.

## Forbidden

- Do not say "done", "fixed", "passes", "ready", or equivalent without fresh evidence.
- Do not invent test commands.
- Do not ignore failing checks.
- Do not report partial verification as full success.
- Do not treat old test output as fresh evidence.
- Do not close the task if verification failed due to this change.

## Exit Criteria

You may leave this phase when:

- Relevant checks passed, or
- Verification failed and the next repair path is clear, or
- Verification could not be run and the limitation is explicitly documented.

## Next Allowed Phases

- `close`
- `diagnose`
- `plan`
- `review`

## User-Facing Response Style

Report:

- command or method;
- result;
- whether it passed or failed;
- relevant notes;
- remaining uncertainty.
"""

PHASE_CLOSE = """# Phase: close

## Purpose

Summarize the completed work, verification, risks, and follow-up suggestions.

This phase is for final user-facing reporting.

## Use This Phase When

- Work has been reviewed and verified, or
- Work cannot proceed and the limitation must be reported, or
- The task was informational and no files were modified.

## Required Actions

1. Summarize the final result.
2. List modified files, or explicitly say none.
3. Summarize verification commands and results.
4. State review result.
5. State deviations, risks, or uncertainty.
6. Suggest follow-ups only when useful.
7. Be honest about anything not verified.
8. Confirm that static type checking was performed and report its result. If type checking was skipped, explicitly state why.

## Forbidden

- Do not claim success if verification did not happen.
- Do not hide failed checks.
- Do not omit risks that affect the user.
- Do not imply files were modified if none were.
- Do not include excessive internal process details.

## Final Summary Format

Use:

```markdown
### Summary

- ...

### Modified Files

- `path/to/file`
  - ...

### Verification

- `command`
  - Result: passed / failed / not run
  - Notes: ...

### Review Result

- ...

### Deviations / Risks

- ...

### Follow-up Suggestions

- ...
```

If no files were modified:

```markdown
### Modified Files

- None.
```

## Exit Criteria

The task is complete from the agent's side.

## Next Allowed Phases

* `understand`
* `diagnose`
* `plan`

## User-Facing Response Style

Be concise, concrete, and honest.
"""

PHASE_INVALID = """# Invalid Phase

The requested phase is not supported.

Supported phases:

- `understand`
- `diagnose`
- `design`
- `plan`
- `await_approval`
- `execute`
- `review`
- `verify`
- `close`

Enter `understand` if unsure.
"""

# ── Phase content dictionary ─────────────────────────────────────────

PHASE_CONTENTS: dict[str, str] = {
    "understand": PHASE_UNDERSTAND,
    "diagnose": PHASE_DIAGNOSE,
    "design": PHASE_DESIGN,
    "plan": PHASE_PLAN,
    "await_approval": PHASE_AWAIT_APPROVAL,
    "execute": PHASE_EXECUTE,
    "review": PHASE_REVIEW,
    "verify": PHASE_VERIFY,
    "close": PHASE_CLOSE,
    "invalid": PHASE_INVALID,
}

VALID_PHASES = frozenset(PHASE_CONTENTS.keys() - {"invalid"})


# ── Tool class ───────────────────────────────────────────────────────

class EnterPhaseTool(CodingToolMixin, BaseTool):
    """进入指定的工作流阶段，获取该阶段的详细指令。"""

    tool_name = "enter_phase"
    tool_description = (
        "Enter a workflow phase to receive detailed instructions for that phase. "
        "Supported phases: understand, diagnose, design, plan, await_approval, "
        "execute, review, verify, close."
    )
    chatter_allow = ["coding_agent"]

    async def execute(
        self,
        phase: Annotated[
            str,
            "The phase name to enter. Must be one of: understand, diagnose, design, "
            "plan, await_approval, execute, review, verify, close.",
        ],
    ) -> tuple[bool, str]:
        """进入工作流阶段并返回详细指令。

        Args:
            phase: 阶段名称

        Returns:
            (成功标志, 阶段指令 markdown 或错误信息)
        """
        phase_key = (phase or "").strip().lower()

        if phase_key not in VALID_PHASES:
            return False, PHASE_CONTENTS["invalid"]

        # 更新 session 中的 phase 状态
        session = self._get_current_session()
        if session is not None:
            session.phase = phase_key

        return True, PHASE_CONTENTS[phase_key]
