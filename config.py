from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AgentConfig:
    model: str = "sonnet"
    permission_mode: str = "bypassPermissions"
    max_turns: int = 50
    reviewer_max_turns: int = 10
    max_budget_usd: float = 5.0
    specialist_tools: list[str] = field(default_factory=lambda: [
        "Read", "Write", "Edit", "Bash", "Glob", "Grep",
    ])
    reviewer_tools: list[str] = field(default_factory=lambda: [
        "Read", "Bash", "Glob", "Grep",
    ])
    planner_tools: list[str] = field(default_factory=lambda: [
        "Read", "Glob", "Grep",
    ])


@dataclass
class PipelineConfig:
    repo_path: Path = field(default_factory=lambda: Path("."))
    worktree_base: str = ".worktrees"
    branch_prefix: str = "agent/"
    main_branch: str = "main"
    max_retries: int = 2
    test_command: str = ""
    agent: AgentConfig = field(default_factory=AgentConfig)
