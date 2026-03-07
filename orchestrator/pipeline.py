from __future__ import annotations

import logging
from pathlib import Path

from config import PipelineConfig
from models.schemas import (
    PipelineResult,
    ReviewResult,
    ReviewVerdict,
    SpecialistResult,
    Subtask,
    SubtaskStatus,
)
from orchestrator.executor import execute_subtask
from orchestrator.merger import merge_approved
from orchestrator.planner import plan_task
from orchestrator.reviewer import review_subtask
from worktree.manager import WorktreeInfo, WorktreeManager

log = logging.getLogger(__name__)


async def run_pipeline(task: str, config: PipelineConfig) -> PipelineResult:
    """Full pipeline: plan -> execute -> review -> merge."""
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

    # Flatten execution order
    all_subtask_ids: list[str] = []
    for group in plan.execution_order:
        for sid in group:
            if sid in subtask_map:
                all_subtask_ids.append(sid)

    # ── Phase B: Execute ─────────────────────────────────────────────
    log.info("=" * 60)
    log.info("PHASE B: EXECUTING (%d subtasks)", len(all_subtask_ids))
    log.info("=" * 60)

    execution_results: list[tuple[Subtask, WorktreeInfo, SpecialistResult]] = []

    for group in plan.execution_order:
        # Phase 1: sequential within each group (parallel comes later)
        for subtask_id in group:
            subtask = subtask_map.get(subtask_id)
            if subtask is None:
                log.warning("Subtask %s in execution_order but not in subtasks — skipping", subtask_id)
                continue

            wt: WorktreeInfo | None = None
            spec_result: SpecialistResult | None = None

            for attempt in range(1 + config.max_retries):
                attempt_id = subtask_id if attempt == 0 else f"{subtask_id}-retry-{attempt}"
                try:
                    wt = worktree_mgr.create(attempt_id)
                    spec_result = await execute_subtask(
                        subtask, wt, plan.original_task, config.agent,
                    )

                    if spec_result.status != SubtaskStatus.FAILED:
                        break  # success or completed

                    # Failed — clean up and retry
                    log.warning(
                        "Subtask [%s] failed (attempt %d/%d): %s",
                        subtask_id,
                        attempt + 1,
                        1 + config.max_retries,
                        spec_result.error_message or spec_result.summary,
                    )
                    worktree_mgr.remove(wt)
                    wt = None

                except Exception as exc:
                    log.error("Unexpected error executing [%s]: %s", subtask_id, exc)
                    if wt is not None:
                        worktree_mgr.remove(wt)
                        wt = None
                    spec_result = SpecialistResult(
                        subtask_id=subtask_id,
                        status=SubtaskStatus.FAILED,
                        summary=f"Unexpected error: {exc}",
                        files_modified=[],
                        tests_passed=False,
                        error_message=str(exc),
                    )

            if wt is not None and spec_result is not None:
                execution_results.append((subtask, wt, spec_result))
            elif spec_result is not None:
                # All retries exhausted, no worktree left
                log.error("Subtask [%s] failed after all retries", subtask_id)

    # ── Phase C: Review ──────────────────────────────────────────────
    log.info("=" * 60)
    log.info("PHASE C: REVIEWING")
    log.info("=" * 60)

    review_results: list[tuple[Subtask, WorktreeInfo, SpecialistResult, ReviewResult]] = []

    for subtask, wt, spec_result in execution_results:
        if spec_result.status == SubtaskStatus.FAILED:
            log.info("Skipping review for failed subtask [%s]", subtask.id)
            worktree_mgr.remove(wt)
            continue

        diff = worktree_mgr.get_diff(wt)
        if not diff.strip():
            log.warning("No diff for subtask [%s] — skipping review", subtask.id)
            worktree_mgr.remove(wt)
            continue

        review = await review_subtask(
            subtask, spec_result, diff, repo_path, config.agent,
        )
        review_results.append((subtask, wt, spec_result, review))

    # ── Phase D: Merge ───────────────────────────────────────────────
    log.info("=" * 60)
    log.info("PHASE D: MERGING")
    log.info("=" * 60)

    approved = [
        (wt, review)
        for _, wt, _, review in review_results
        if review.verdict == ReviewVerdict.APPROVED
    ]
    rejected = [
        (wt, review)
        for _, wt, _, review in review_results
        if review.verdict != ReviewVerdict.APPROVED
    ]

    # Log rejected subtasks
    for wt, review in rejected:
        log.warning(
            "REJECTED [%s]: %s — %s",
            review.subtask_id,
            review.verdict.value,
            review.reasoning[:200],
        )
        for issue in review.issues:
            log.warning("  Issue: %s", issue)
        worktree_mgr.remove(wt)

    merged_branches = merge_approved(approved, worktree_mgr, test_command)

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

    result = PipelineResult(
        task=task,
        subtasks_total=total,
        subtasks_completed=completed,
        subtasks_failed=failed,
        branches_merged=merged_branches,
        branches_rejected=[r.subtask_id for _, r in rejected],
        summary=" ".join(summary_parts),
    )

    log.info("=" * 60)
    log.info("PIPELINE COMPLETE: %s", result.summary)
    log.info("=" * 60)

    return result
