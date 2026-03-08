from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from config import PipelineConfig
from models.schemas import (
    PipelineResult,
    ReviewResult,
    ReviewVerdict,
    SecurityReviewResult,
    SecuritySeverity,
    SecurityVerdict,
    SpecialistResult,
    Subtask,
    SubtaskStatus,
)
from orchestrator.executor import execute_subtask, execute_subtask_revision
from orchestrator.merger import merge_approved
from orchestrator.planner import plan_task
from orchestrator.reviewer import review_subtask
from orchestrator.security_reviewer import security_review_subtask
from worktree.manager import WorktreeInfo, WorktreeManager

log = logging.getLogger(__name__)

# Type alias for the review results tuple
ReviewTuple = tuple[
    Subtask, WorktreeInfo, SpecialistResult, ReviewResult, SecurityReviewResult | None
]

# ── Helpers ─────────────────────────────────────────────────────────


async def _execute_one_subtask(
    subtask: Subtask,
    worktree_mgr: WorktreeManager,
    overall_context: str,
    config: PipelineConfig,
) -> tuple[Subtask, WorktreeInfo | None, SpecialistResult | None]:
    """Execute a single subtask with retry logic. Returns (subtask, wt, result)."""
    wt: WorktreeInfo | None = None
    spec_result: SpecialistResult | None = None

    for attempt in range(1 + config.max_retries):
        attempt_id = subtask.id if attempt == 0 else f"{subtask.id}-retry-{attempt}"
        try:
            wt = worktree_mgr.create(attempt_id)
            spec_result = await execute_subtask(
                subtask, wt, overall_context, config.agent,
            )

            if spec_result.status != SubtaskStatus.FAILED:
                break  # success or completed

            # Failed — clean up and retry
            log.warning(
                "Subtask [%s] failed (attempt %d/%d): %s",
                subtask.id,
                attempt + 1,
                1 + config.max_retries,
                spec_result.error_message or spec_result.summary,
            )
            worktree_mgr.remove(wt)
            wt = None

        except Exception as exc:
            log.error("Unexpected error executing [%s]: %s", subtask.id, exc)
            if wt is not None:
                worktree_mgr.remove(wt)
                wt = None
            spec_result = SpecialistResult(
                subtask_id=subtask.id,
                status=SubtaskStatus.FAILED,
                summary=f"Unexpected error: {exc}",
                files_modified=[],
                tests_passed=False,
                error_message=str(exc),
            )

    if wt is None and spec_result is not None:
        log.error("Subtask [%s] failed after all retries", subtask.id)

    return subtask, wt, spec_result


async def _review_and_revise(
    subtask: Subtask,
    wt: WorktreeInfo,
    spec_result: SpecialistResult,
    worktree_mgr: WorktreeManager,
    repo_path: Path,
    overall_context: str,
    config: PipelineConfig,
    max_revision_rounds: int = 1,
) -> ReviewTuple | None:
    """Review a subtask; if NEEDS_CHANGES or security FAIL, revise and re-review."""
    if spec_result.status == SubtaskStatus.FAILED:
        log.info("Skipping review for failed subtask [%s]", subtask.id)
        worktree_mgr.remove(wt)
        return None

    current_wt = wt
    current_result = spec_result
    security_enabled = config.security_review

    for round_num in range(1 + max_revision_rounds):
        diff = worktree_mgr.get_diff(current_wt)
        if not diff.strip():
            log.warning("No diff for subtask [%s] — skipping review", subtask.id)
            worktree_mgr.remove(current_wt)
            return None

        # ── Run code review (and security review if enabled) in parallel ──
        review_coro = review_subtask(
            subtask, current_result, diff, repo_path, config.agent,
        )

        if security_enabled:
            sec_coro = security_review_subtask(
                subtask, current_result, diff, repo_path, config.agent,
            )
            review, sec_review = await asyncio.gather(review_coro, sec_coro)
        else:
            review = await review_coro
            sec_review = None

        # ── Determine if revision is needed ──
        code_needs_revision = review.verdict == ReviewVerdict.NEEDS_CHANGES
        security_needs_revision = (
            sec_review is not None
            and sec_review.verdict == SecurityVerdict.FAIL
        )

        if not code_needs_revision and not security_needs_revision:
            # APPROVED or REJECTED from code review + PASS/WARN/None from security
            return subtask, current_wt, current_result, review, sec_review

        if round_num >= max_revision_rounds:
            # Out of revision rounds — return as-is
            log.info(
                "Subtask [%s] still needs changes after %d revision(s) — accepting as-is",
                subtask.id,
                max_revision_rounds,
            )
            return subtask, current_wt, current_result, review, sec_review

        # ── Revision: re-execute specialist with combined feedback ──
        reasons = []
        if code_needs_revision:
            reasons.append("code review")
        if security_needs_revision:
            reasons.append("security review")
        log.info(
            "Subtask [%s] needs changes from %s (round %d/%d) — revising",
            subtask.id,
            " + ".join(reasons),
            round_num + 1,
            max_revision_rounds,
        )

        # Clean up old worktree, create fresh one for revision
        worktree_mgr.remove(current_wt)
        revision_id = f"{subtask.id}-rev-{round_num + 1}"

        try:
            current_wt = worktree_mgr.create(revision_id)
            current_result = await execute_subtask_revision(
                subtask, current_wt, overall_context, review, config.agent,
                security_review=sec_review,
            )

            if current_result.status == SubtaskStatus.FAILED:
                log.warning("Revision for [%s] failed — aborting revisions", subtask.id)
                worktree_mgr.remove(current_wt)
                return None

        except Exception as exc:
            log.error("Revision crashed for [%s]: %s", subtask.id, exc)
            worktree_mgr.remove(current_wt)
            return None

    # Should not reach here, but just in case
    return subtask, current_wt, current_result, review, sec_review


def _format_security_summary(
    review_results: list[ReviewTuple],
) -> list[str]:
    """Build human-readable security findings summary lines."""
    lines: list[str] = []
    for _, _, _, _, sec_review in review_results:
        if sec_review is None or not sec_review.findings:
            continue
        for f in sec_review.findings:
            cwe = f" [{f.cwe_id}]" if f.cwe_id else ""
            lines.append(
                f"{f.severity.value.upper()}{cwe} at {f.location}: "
                f"{f.description}"
            )
    return lines


# ── Main Pipeline ───────────────────────────────────────────────────


async def run_pipeline(task: str, config: PipelineConfig) -> PipelineResult:
    """Full pipeline: plan -> execute (parallel) -> review+revise -> merge."""
    repo_path = config.repo_path.resolve()
    worktree_mgr = WorktreeManager(
        repo_path,
        config.worktree_base,
        config.branch_prefix,
    )

    # ── Phase A: Plan ────────────────────────────────────────────────
    log.info("=" * 60)
    log.info("PHASE A: PLANNING")
    log.info("=" * 60)

    plan = await plan_task(task, repo_path, config.agent)
    test_command = config.test_command or plan.test_command

    # Build lookup for subtasks by id
    subtask_map: dict[str, Subtask] = {st.id: st for st in plan.subtasks}

    # ── Phase B: Execute ─────────────────────────────────────────────
    log.info("=" * 60)
    log.info("PHASE B: EXECUTING (%d subtasks)", len(subtask_map))
    log.info("=" * 60)

    execution_results: list[tuple[Subtask, WorktreeInfo, SpecialistResult]] = []

    for group_idx, group in enumerate(plan.execution_order):
        valid_subtasks = [
            subtask_map[sid] for sid in group if sid in subtask_map
        ]
        if not valid_subtasks:
            continue

        if len(valid_subtasks) == 1:
            log.info("Group %d: executing [%s]", group_idx + 1, valid_subtasks[0].id)
            subtask, wt, spec_result = await _execute_one_subtask(
                valid_subtasks[0], worktree_mgr, plan.original_task, config,
            )
            if wt is not None and spec_result is not None:
                execution_results.append((subtask, wt, spec_result))
        else:
            log.info(
                "Group %d: executing %d subtasks in parallel: %s",
                group_idx + 1,
                len(valid_subtasks),
                ", ".join(s.id for s in valid_subtasks),
            )
            coros = [
                _execute_one_subtask(st, worktree_mgr, plan.original_task, config)
                for st in valid_subtasks
            ]
            results = await asyncio.gather(*coros, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    log.error("Parallel execution raised: %s", r)
                    continue
                subtask, wt, spec_result = r
                if wt is not None and spec_result is not None:
                    execution_results.append((subtask, wt, spec_result))

    # ── Phase C: Review + Revise ─────────────────────────────────────
    log.info("=" * 60)
    review_label = "REVIEWING + SECURITY SCAN" if config.security_review else "REVIEWING"
    log.info("PHASE C: %s (%d completed subtasks)", review_label, len(execution_results))
    log.info("=" * 60)

    review_results: list[ReviewTuple] = []

    # Reviews can run in parallel (each reviewer is read-only).
    # But revisions are sequential per subtask (handled inside _review_and_revise).
    if len(execution_results) == 1:
        result = await _review_and_revise(
            *execution_results[0],
            worktree_mgr, repo_path, plan.original_task, config,
            max_revision_rounds=config.max_retries,
        )
        if result is not None:
            review_results.append(result)
    else:
        log.info(
            "Reviewing %d subtasks in parallel: %s",
            len(execution_results),
            ", ".join(s.id for s, _, _ in execution_results),
        )
        coros = [
            _review_and_revise(
                st, wt, sr,
                worktree_mgr, repo_path, plan.original_task, config,
                max_revision_rounds=config.max_retries,
            )
            for st, wt, sr in execution_results
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                log.error("Parallel review raised: %s", r)
                continue
            if r is not None:
                review_results.append(r)

    # ── Phase D: Merge ───────────────────────────────────────────────
    log.info("=" * 60)
    log.info("PHASE D: MERGING")
    log.info("=" * 60)

    # Classify subtasks: mergeable vs rejected vs security-blocked.
    # Security FAIL blocks merge regardless of code review verdict.
    mergeable: list[tuple[WorktreeInfo, ReviewResult]] = []
    rejected: list[tuple[WorktreeInfo, ReviewResult]] = []
    security_blocked: list[tuple[WorktreeInfo, ReviewResult, SecurityReviewResult]] = []

    for _, wt, _, review, sec_review in review_results:
        # Security FAIL blocks regardless of code review
        if sec_review is not None and sec_review.verdict == SecurityVerdict.FAIL:
            security_blocked.append((wt, review, sec_review))
            continue

        if review.verdict == ReviewVerdict.REJECTED:
            rejected.append((wt, review))
            continue

        # Both code review (APPROVED/NEEDS_CHANGES) and security (PASS/WARN/None)
        mergeable.append((wt, review))

    # Log needs_changes notes (still merging)
    for wt, review in mergeable:
        if review.verdict == ReviewVerdict.NEEDS_CHANGES:
            log.warning(
                "NEEDS_CHANGES [%s] (merging with notes): %s",
                review.subtask_id,
                review.reasoning[:200],
            )
            for issue in review.issues:
                log.warning("  Note: %s", issue)

    # Log security warnings for merged subtasks
    for _, wt, _, review, sec_review in review_results:
        if sec_review is None or sec_review.verdict != SecurityVerdict.WARN:
            continue
        log.warning(
            "SECURITY WARN [%s]: %s",
            sec_review.subtask_id,
            sec_review.summary[:200],
        )
        for finding in sec_review.findings:
            if finding.severity == SecuritySeverity.MEDIUM:
                cwe = f" [{finding.cwe_id}]" if finding.cwe_id else ""
                log.warning(
                    "  %s%s at %s: %s",
                    finding.severity.value.upper(),
                    cwe,
                    finding.location,
                    finding.description,
                )

    # Log rejected subtasks
    for wt, review in rejected:
        log.warning(
            "REJECTED [%s]: %s",
            review.subtask_id,
            review.reasoning[:200],
        )
        for issue in review.issues:
            log.warning("  Issue: %s", issue)
        worktree_mgr.remove(wt)

    # Log and clean up security-blocked subtasks
    for wt, review, sec_review in security_blocked:
        log.error(
            "SECURITY BLOCKED [%s]: %s",
            sec_review.subtask_id,
            sec_review.summary[:200],
        )
        for finding in sec_review.findings:
            if finding.severity in (SecuritySeverity.CRITICAL, SecuritySeverity.HIGH):
                cwe = f" [{finding.cwe_id}]" if finding.cwe_id else ""
                log.error(
                    "  %s%s at %s: %s",
                    finding.severity.value.upper(),
                    cwe,
                    finding.location,
                    finding.description,
                )
        worktree_mgr.remove(wt)

    merged_branches = merge_approved(mergeable, worktree_mgr, test_command)

    # ── Summary ──────────────────────────────────────────────────────
    total = len(plan.subtasks)
    completed = len(merged_branches)
    failed = total - completed

    summary_parts = [f"Completed {completed}/{total} subtasks."]
    if merged_branches:
        summary_parts.append(f"Merged: {', '.join(merged_branches)}")
    if rejected:
        rejected_ids = [r.subtask_id for _, r in rejected]
        summary_parts.append(f"Rejected: {', '.join(rejected_ids)}")
    if security_blocked:
        blocked_ids = [sr.subtask_id for _, _, sr in security_blocked]
        summary_parts.append(f"Security blocked: {', '.join(blocked_ids)}")

    result = PipelineResult(
        task=task,
        subtasks_total=total,
        subtasks_completed=completed,
        subtasks_failed=failed,
        branches_merged=merged_branches,
        branches_rejected=[r.subtask_id for _, r in rejected],
        security_blocked=[sr.subtask_id for _, _, sr in security_blocked],
        security_findings_summary=_format_security_summary(review_results),
        summary=" ".join(summary_parts),
    )

    log.info("=" * 60)
    log.info("PIPELINE COMPLETE: %s", result.summary)
    log.info("=" * 60)

    return result
