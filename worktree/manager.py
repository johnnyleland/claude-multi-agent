from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class WorktreeInfo:
    path: Path
    branch: str
    base_commit: str


class WorktreeError(Exception):
    pass


class WorktreeManager:
    def __init__(
        self,
        repo_path: Path,
        worktree_base: str = ".worktrees",
        branch_prefix: str = "agent/",
    ) -> None:
        self.repo_path = repo_path.resolve()
        self.worktree_dir = self.repo_path / worktree_base
        self.branch_prefix = branch_prefix

    def _run(
        self,
        args: list[str],
        cwd: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        cwd = cwd or self.repo_path
        log.debug("git %s (cwd=%s)", " ".join(args), cwd)
        return subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=check,
        )

    def _head_commit(self, cwd: Path | None = None) -> str:
        result = self._run(["rev-parse", "HEAD"], cwd=cwd)
        return result.stdout.strip()

    def _main_branch(self) -> str:
        """Detect the default branch (main or master)."""
        for name in ("main", "master"):
            r = self._run(["rev-parse", "--verify", name], check=False)
            if r.returncode == 0:
                return name
        return "main"

    def create(self, subtask_id: str) -> WorktreeInfo:
        """Create a git worktree on a new branch for the given subtask."""
        self.worktree_dir.mkdir(parents=True, exist_ok=True)

        branch = f"{self.branch_prefix}{subtask_id}"
        wt_path = self.worktree_dir / subtask_id
        base_commit = self._head_commit()

        # Clean up stale worktree/branch if they exist
        if wt_path.exists():
            log.warning("Removing stale worktree at %s", wt_path)
            self._run(["worktree", "remove", "--force", str(wt_path)], check=False)

        r = self._run(["branch", "--list", branch])
        if branch in r.stdout:
            self._run(["branch", "-D", branch], check=False)

        result = self._run([
            "worktree", "add", "-b", branch, str(wt_path), base_commit,
        ])
        if result.returncode != 0:
            raise WorktreeError(
                f"Failed to create worktree: {result.stderr}"
            )

        info = WorktreeInfo(path=wt_path, branch=branch, base_commit=base_commit)
        log.info("Created worktree: %s (branch=%s, base=%s)", wt_path, branch, base_commit[:8])
        return info

    def get_diff(self, info: WorktreeInfo) -> str:
        """Get the diff of all changes in the worktree since the base commit."""
        result = self._run(
            ["diff", f"{info.base_commit}..HEAD"],
            cwd=info.path,
        )
        return result.stdout

    def get_diff_stat(self, info: WorktreeInfo) -> str:
        """Get a summary stat of changes."""
        result = self._run(
            ["diff", "--stat", f"{info.base_commit}..HEAD"],
            cwd=info.path,
        )
        return result.stdout

    def has_changes(self, info: WorktreeInfo) -> bool:
        """Check whether the worktree has any committed changes beyond the base."""
        head = self._head_commit(cwd=info.path)
        return head != info.base_commit

    def merge_to_main(self, info: WorktreeInfo) -> bool:
        """Merge the worktree branch into the main branch.

        Returns True on success, False on merge conflict.
        """
        main = self._main_branch()

        # Checkout main in the repo root
        self._run(["checkout", main])

        result = self._run(
            ["merge", "--no-ff", info.branch, "-m", f"agent: merge {info.branch}"],
            check=False,
        )

        if result.returncode != 0:
            log.error("Merge conflict for %s: %s", info.branch, result.stderr)
            self._run(["merge", "--abort"], check=False)
            return False

        log.info("Merged %s into %s", info.branch, main)
        return True

    def remove(self, info: WorktreeInfo) -> None:
        """Remove a worktree and clean up its branch."""
        if info.path.exists():
            self._run(["worktree", "remove", "--force", str(info.path)], check=False)

        self._run(["worktree", "prune"], check=False)

        # Delete the branch (use -d so unmerged branches raise an error we can ignore)
        self._run(["branch", "-D", info.branch], check=False)

        log.info("Removed worktree: %s", info.path)

    def cleanup_all(self) -> None:
        """Remove all worktrees under the worktree directory."""
        if not self.worktree_dir.exists():
            return

        result = self._run(["worktree", "list", "--porcelain"])
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                wt_path = Path(line.split(" ", 1)[1])
                if str(wt_path).startswith(str(self.worktree_dir)):
                    self._run(
                        ["worktree", "remove", "--force", str(wt_path)],
                        check=False,
                    )

        self._run(["worktree", "prune"], check=False)

        # Clean up agent branches
        result = self._run(["branch", "--list", f"{self.branch_prefix}*"])
        for line in result.stdout.splitlines():
            branch = line.strip().lstrip("* ")
            if branch:
                self._run(["branch", "-D", branch], check=False)

        log.info("Cleaned up all agent worktrees")
