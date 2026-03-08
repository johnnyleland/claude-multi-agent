from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path

from models.schemas import ReviewResult, ReviewVerdict
from worktree.manager import WorktreeInfo, WorktreeManager

log = logging.getLogger(__name__)


def _resolve_test_command(test_command: str) -> str:
    """Replace bare 'python' in a test command with the current interpreter.

    This ensures the merger uses the same Python that runs the orchestrator,
    even when 'python' is not on PATH (common on Windows).
    """
    match = re.match(r"^(python3?)\b", test_command)
    if match:
        return f'"{sys.executable}"' + test_command[match.end():]
    # If the command is just 'pytest ...' without python prefix,
    # rewrite it as '<python> -m pytest ...'
    if test_command.strip().startswith("pytest"):
        return f'"{sys.executable}" -m {test_command}'
    return test_command


def _run_tests(test_command: str, cwd: Path) -> bool:
    """Run the test command and return True if it passes."""
    if not test_command:
        return True

    resolved = _resolve_test_command(test_command)
    log.info("Running tests: %s", resolved)
    result = subprocess.run(
        resolved,
        shell=True,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.error("Tests failed:\n%s\n%s", result.stdout[-2000:], result.stderr[-2000:])
        return False

    log.info("Tests passed")
    return True


def merge_approved(
    approved: list[tuple[WorktreeInfo, ReviewResult]],
    worktree_mgr: WorktreeManager,
    test_command: str,
) -> list[str]:
    """Merge approved branches to main sequentially.

    For each approved result:
    1. Merge branch to main
    2. If test_command provided, run tests on main
    3. If tests fail, revert the merge
    4. Clean up worktree

    Returns list of successfully merged branch names.
    """
    merged: list[str] = []

    for wt_info, review in approved:
        if not worktree_mgr.has_changes(wt_info):
            log.warning(
                "Skipping [%s]: no committed changes in worktree",
                review.subtask_id,
            )
            worktree_mgr.remove(wt_info)
            continue

        success = worktree_mgr.merge_to_main(wt_info)
        if not success:
            log.error(
                "Merge conflict for [%s] branch %s — skipping",
                review.subtask_id,
                wt_info.branch,
            )
            worktree_mgr.remove(wt_info)
            continue

        # Run tests after merge
        if test_command and not _run_tests(test_command, worktree_mgr.repo_path):
            log.error(
                "Tests failed after merging [%s] — reverting",
                review.subtask_id,
            )
            # Revert the merge commit
            subprocess.run(
                ["git", "reset", "--hard", "HEAD~1"],
                cwd=str(worktree_mgr.repo_path),
                capture_output=True,
            )
            worktree_mgr.remove(wt_info)
            continue

        merged.append(wt_info.branch)
        worktree_mgr.remove(wt_info)
        log.info("Successfully merged and cleaned up [%s]", review.subtask_id)

    return merged
