from __future__ import annotations

import logging
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from config import AgentConfig
from models.schemas import (
    SecurityReviewResult,
    SecurityVerdict,
    SpecialistResult,
    Subtask,
)
from prompts.security_reviewer import SECURITY_REVIEWER_SYSTEM_PROMPT

log = logging.getLogger(__name__)


def _build_security_review_prompt(
    subtask: Subtask,
    specialist_result: SpecialistResult,
    diff: str,
) -> str:
    """Build the prompt sent to the security review agent."""
    lines = [
        "## Security Review Request",
        f"**Subtask ID:** {subtask.id}",
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
    lines.append(
        "\nReview these changes for security vulnerabilities and provide your verdict."
    )

    return "\n".join(lines)


async def security_review_subtask(
    subtask: Subtask,
    specialist_result: SpecialistResult,
    diff: str,
    repo_path: Path,
    config: AgentConfig,
) -> SecurityReviewResult:
    """Call the security reviewer agent to scan for vulnerabilities."""
    log.info("Security reviewing subtask [%s] %s", subtask.id, subtask.title)

    prompt = _build_security_review_prompt(subtask, specialist_result, diff)
    result: ResultMessage | None = None

    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                system_prompt=SECURITY_REVIEWER_SYSTEM_PROMPT,
                allowed_tools=config.security_reviewer_tools,
                permission_mode=config.permission_mode,
                cwd=str(repo_path),
                model=config.model,
                max_turns=config.security_reviewer_max_turns,
                max_budget_usd=config.max_budget_usd,
                output_format={
                    "type": "json_schema",
                    "schema": SecurityReviewResult.model_json_schema(),
                },
            ),
        ):
            if isinstance(message, ResultMessage):
                result = message
    except Exception as exc:
        log.error("Security reviewer crashed for [%s]: %s", subtask.id, exc)
        # Fail-closed: crash → FAIL, not PASS
        return SecurityReviewResult(
            subtask_id=subtask.id,
            verdict=SecurityVerdict.FAIL,
            findings=[],
            summary=f"Security review could not be completed: {exc}",
        )

    if result is None or result.is_error or result.structured_output is None:
        reason = result.result if result else "no result"
        log.error("Security reviewer failed for [%s]: %s", subtask.id, reason)
        # Fail-closed: error → FAIL
        return SecurityReviewResult(
            subtask_id=subtask.id,
            verdict=SecurityVerdict.FAIL,
            findings=[],
            summary=f"Security review could not be completed: {reason}",
        )

    review = SecurityReviewResult.model_validate(result.structured_output)
    log.info(
        "Security review [%s]: verdict=%s, findings=%d",
        subtask.id,
        review.verdict.value,
        len(review.findings),
    )
    return review
