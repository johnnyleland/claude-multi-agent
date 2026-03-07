from __future__ import annotations

import logging
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from config import AgentConfig
from models.schemas import TaskPlan
from prompts.planner import PLANNER_SYSTEM_PROMPT

log = logging.getLogger(__name__)


class PlanningError(Exception):
    pass


async def plan_task(
    task_description: str,
    repo_path: Path,
    config: AgentConfig,
) -> TaskPlan:
    """Call the planning agent to decompose a task into subtasks.

    The planner has read-only access to the repo so it can inspect
    code structure before planning.
    """
    log.info("Planning task: %s", task_description[:100])

    result: ResultMessage | None = None

    async for message in query(
        prompt=task_description,
        options=ClaudeAgentOptions(
            system_prompt=PLANNER_SYSTEM_PROMPT,
            allowed_tools=config.planner_tools,
            permission_mode=config.permission_mode,
            cwd=str(repo_path),
            model=config.model,
            max_turns=config.max_turns,
            max_budget_usd=config.max_budget_usd,
            output_format={
                "type": "json_schema",
                "schema": TaskPlan.model_json_schema(),
            },
        ),
    ):
        if isinstance(message, ResultMessage):
            result = message

    if result is None:
        raise PlanningError("Planning agent returned no result")

    if result.is_error:
        raise PlanningError(
            f"Planning agent failed (reason={result.stop_reason}): {result.result}"
        )

    if result.structured_output is None:
        raise PlanningError(
            f"Planning agent returned no structured output: {result.result}"
        )

    plan = TaskPlan.model_validate(result.structured_output)

    log.info(
        "Plan created: %d subtasks, %d execution groups, test_command=%r",
        len(plan.subtasks),
        len(plan.execution_order),
        plan.test_command or "(none)",
    )
    for st in plan.subtasks:
        log.info("  [%s] %s", st.id, st.title)

    return plan
