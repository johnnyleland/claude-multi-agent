from __future__ import annotations

import logging
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from config import AgentConfig
from models.schemas import ReviewResult, ReviewVerdict, SpecialistResult, Subtask
from prompts.reviewer import REVIEWER_SYSTEM_PROMPT

log = logging.getLogger(__name__)


def _build_review_prompt(
    subtask: Subtask,
    specialist_result: SpecialistResult,
    diff: str,
) -> str:
    """Build the prompt sent to the review agent."""
    lines = [
        "## Subtask Under Review",
        f"**ID:** {subtask.id}",
        f"**Title:** {subtask.title}",
        f"\n**Description:**\n{subtask.description}",
        "\n**Acceptance Criteria:**\n"
        + "\n".join(f"- {c}" for c in subtask.acceptance_criteria),
        "\n## Specialist Self-Report",
        f"**Status:** {specialist_result.status.value}",
        f"**Summary:** {specialist_result.summary}",
        f"**Tests passed:** {specialist_result.tests_passed}",
        f"**Files modified:** {', '.join(specialist_result.files_modified) or '(none)'}",
    ]

    if specialist_result.test_output:
        lines.append(f"\n**Test output:**\n```\n{specialist_result.test_output}\n```")

    if specialist_result.error_message:
        lines.append(f"\n**Error:** {specialist_result.error_message}")

    # Truncate very large diffs to avoid blowing context
    max_diff_chars = 50_000
    if len(diff) > max_diff_chars:
        diff = diff[:max_diff_chars] + "\n\n... (diff truncated, use tools to read full files)"

    lines.append(f"\n## Git Diff\n```diff\n{diff}\n```")
    lines.append("\nReview these changes and provide your verdict.")

    return "\n".join(lines)


async def review_subtask(
    subtask: Subtask,
    specialist_result: SpecialistResult,
    diff: str,
    repo_path: Path,
    config: AgentConfig,
) -> ReviewResult:
    """Call the reviewer agent to evaluate a completed subtask."""
    log.info("Reviewing subtask [%s] %s", subtask.id, subtask.title)

    prompt = _build_review_prompt(subtask, specialist_result, diff)
    result: ResultMessage | None = None

    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                system_prompt=REVIEWER_SYSTEM_PROMPT,
                allowed_tools=config.reviewer_tools,
                permission_mode=config.permission_mode,
                cwd=str(repo_path),
                model=config.model,
                max_turns=30,
                max_budget_usd=config.max_budget_usd,
                output_format={
                    "type": "json_schema",
                    "schema": ReviewResult.model_json_schema(),
                },
            ),
        ):
            if isinstance(message, ResultMessage):
                result = message
    except Exception as exc:
        log.error("Reviewer agent crashed for [%s]: %s", subtask.id, exc)
        return ReviewResult(
            subtask_id=subtask.id,
            verdict=ReviewVerdict.REJECTED,
            issues=[f"Reviewer agent crashed: {exc}"],
            reasoning="Review could not be completed due to an error.",
        )

    if result is None or result.is_error or result.structured_output is None:
        reason = result.result if result else "no result"
        log.error("Reviewer agent failed for [%s]: %s", subtask.id, reason)
        return ReviewResult(
            subtask_id=subtask.id,
            verdict=ReviewVerdict.REJECTED,
            issues=[f"Reviewer agent failed: {reason}"],
            reasoning="Review could not be completed.",
        )

    review = ReviewResult.model_validate(result.structured_output)
    log.info(
        "Review [%s]: verdict=%s, issues=%d",
        subtask.id,
        review.verdict.value,
        len(review.issues),
    )
    return review
