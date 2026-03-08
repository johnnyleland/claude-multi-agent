from __future__ import annotations

import logging
import os
import sys

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from config import AgentConfig
from models.schemas import ReviewResult, SpecialistResult, Subtask, SubtaskStatus
from prompts.specialist import SPECIALIST_SYSTEM_PROMPT
from worktree.manager import WorktreeInfo

log = logging.getLogger(__name__)


def _python_env() -> dict[str, str]:
    """Build env dict that puts the orchestrator's Python on PATH."""
    python_dir = str(os.path.dirname(sys.executable))
    current_path = os.environ.get("PATH", "")
    return {"PATH": f"{python_dir}{os.pathsep}{current_path}"}


def _build_specialist_prompt(subtask: Subtask, overall_context: str) -> str:
    """Build the prompt sent to the specialist agent."""
    lines = [
        f"## Overall Task Context\n{overall_context}",
        f"\n## Your Subtask: {subtask.title}",
        f"**ID:** {subtask.id}",
        f"\n**Description:**\n{subtask.description}",
    ]

    if subtask.files_likely_affected:
        lines.append(
            "\n**Files likely affected:**\n"
            + "\n".join(f"- {f}" for f in subtask.files_likely_affected)
        )

    lines.append(
        "\n**Acceptance Criteria:**\n"
        + "\n".join(f"- {c}" for c in subtask.acceptance_criteria)
    )

    # Tell the specialist which Python to use for running tests
    python_exe = sys.executable
    lines.append(
        f"\n**Environment note:** Use `\"{python_exe}\" -m pytest` to run tests "
        f"(do NOT use bare `python` — it may not be on PATH)."
    )

    lines.append(
        "\nComplete this subtask. Commit your changes and report the result."
    )

    return "\n".join(lines)


async def execute_subtask(
    subtask: Subtask,
    worktree_info: WorktreeInfo,
    overall_context: str,
    config: AgentConfig,
) -> SpecialistResult:
    """Run a specialist agent in the given worktree."""
    log.info(
        "Executing subtask [%s] %s in %s",
        subtask.id,
        subtask.title,
        worktree_info.path,
    )

    prompt = _build_specialist_prompt(subtask, overall_context)
    result: ResultMessage | None = None

    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                system_prompt=SPECIALIST_SYSTEM_PROMPT,
                allowed_tools=config.specialist_tools,
                permission_mode=config.permission_mode,
                cwd=str(worktree_info.path),
                model=config.model,
                max_turns=config.max_turns,
                max_budget_usd=config.max_budget_usd,
                output_format={
                    "type": "json_schema",
                    "schema": SpecialistResult.model_json_schema(),
                },
                # Ensure the specialist's Bash tool can find `python`
                env=_python_env(),
            ),
        ):
            if isinstance(message, ResultMessage):
                result = message
    except Exception as exc:
        log.error("Specialist agent crashed for [%s]: %s", subtask.id, exc)
        return SpecialistResult(
            subtask_id=subtask.id,
            status=SubtaskStatus.FAILED,
            summary="Agent execution crashed",
            files_modified=[],
            tests_passed=False,
            error_message=str(exc),
        )

    if result is None or result.is_error:
        reason = result.result if result else "no result returned"
        log.error("Specialist agent failed for [%s]: %s", subtask.id, reason)
        return SpecialistResult(
            subtask_id=subtask.id,
            status=SubtaskStatus.FAILED,
            summary="Agent execution failed",
            files_modified=[],
            tests_passed=False,
            error_message=reason,
        )

    if result.structured_output is None:
        log.error(
            "Specialist returned no structured output for [%s]: %s",
            subtask.id,
            result.result,
        )
        return SpecialistResult(
            subtask_id=subtask.id,
            status=SubtaskStatus.FAILED,
            summary="Agent returned no structured output",
            files_modified=[],
            tests_passed=False,
            error_message=result.result,
        )

    specialist_result = SpecialistResult.model_validate(result.structured_output)
    log.info(
        "Subtask [%s] %s: status=%s, tests_passed=%s, files=%d",
        subtask.id,
        subtask.title,
        specialist_result.status.value,
        specialist_result.tests_passed,
        len(specialist_result.files_modified),
    )
    return specialist_result


def _build_revision_prompt(
    subtask: Subtask,
    overall_context: str,
    review: ReviewResult,
) -> str:
    """Build a prompt for re-running a specialist with reviewer feedback."""
    base = _build_specialist_prompt(subtask, overall_context)

    feedback_lines = [
        "\n## Reviewer Feedback (you must address these issues)",
        f"**Verdict:** {review.verdict.value}",
        f"**Reasoning:** {review.reasoning}",
    ]
    if review.issues:
        feedback_lines.append(
            "\n**Issues to fix:**\n"
            + "\n".join(f"- {issue}" for issue in review.issues)
        )
    if review.suggestions:
        feedback_lines.append(
            "\n**Suggestions:**\n"
            + "\n".join(f"- {s}" for s in review.suggestions)
        )
    feedback_lines.append(
        "\nYou are re-doing this subtask. Your previous attempt was reviewed "
        "and the reviewer found issues listed above. Address ALL of them, "
        "then commit and report the result."
    )

    return base + "\n".join(feedback_lines)


async def execute_subtask_revision(
    subtask: Subtask,
    worktree_info: WorktreeInfo,
    overall_context: str,
    review: ReviewResult,
    config: AgentConfig,
) -> SpecialistResult:
    """Re-run a specialist with reviewer feedback in a fresh worktree."""
    log.info(
        "Re-executing subtask [%s] %s with reviewer feedback in %s",
        subtask.id,
        subtask.title,
        worktree_info.path,
    )

    prompt = _build_revision_prompt(subtask, overall_context, review)
    result: ResultMessage | None = None

    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                system_prompt=SPECIALIST_SYSTEM_PROMPT,
                allowed_tools=config.specialist_tools,
                permission_mode=config.permission_mode,
                cwd=str(worktree_info.path),
                model=config.model,
                max_turns=config.max_turns,
                max_budget_usd=config.max_budget_usd,
                output_format={
                    "type": "json_schema",
                    "schema": SpecialistResult.model_json_schema(),
                },
                env=_python_env(),
            ),
        ):
            if isinstance(message, ResultMessage):
                result = message
    except Exception as exc:
        log.error("Revision agent crashed for [%s]: %s", subtask.id, exc)
        return SpecialistResult(
            subtask_id=subtask.id,
            status=SubtaskStatus.FAILED,
            summary="Revision agent crashed",
            files_modified=[],
            tests_passed=False,
            error_message=str(exc),
        )

    if result is None or result.is_error:
        reason = result.result if result else "no result returned"
        return SpecialistResult(
            subtask_id=subtask.id,
            status=SubtaskStatus.FAILED,
            summary="Revision agent failed",
            files_modified=[],
            tests_passed=False,
            error_message=reason,
        )

    if result.structured_output is None:
        return SpecialistResult(
            subtask_id=subtask.id,
            status=SubtaskStatus.FAILED,
            summary="Revision agent returned no structured output",
            files_modified=[],
            tests_passed=False,
            error_message=result.result,
        )

    specialist_result = SpecialistResult.model_validate(result.structured_output)
    log.info(
        "Revision [%s] %s: status=%s, tests_passed=%s, files=%d",
        subtask.id,
        subtask.title,
        specialist_result.status.value,
        specialist_result.tests_passed,
        len(specialist_result.files_modified),
    )
    return specialist_result
