#!/usr/bin/env python3
"""Multi-agent orchestrator — CLI entry point.

Usage:
    python run.py "Fix the bug in calculator.py and add tests" --repo /path/to/repo
    python run.py "Add input validation to all API endpoints" --repo . --model opus
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from config import AgentConfig, PipelineConfig
from orchestrator.pipeline import run_pipeline
from utils import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Multi-agent orchestrator: plan → execute → review → merge",
    )
    parser.add_argument(
        "task",
        help="High-level task description",
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Path to the target git repository (default: current directory)",
    )
    parser.add_argument(
        "--model",
        default="sonnet",
        help="Claude model to use (default: sonnet)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Max retry attempts per subtask (default: 2)",
    )
    parser.add_argument(
        "--test-command",
        default="",
        help="Override auto-detected test command (e.g. 'pytest', 'npm test')",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=5.0,
        help="Max USD budget per agent call (default: 5.0)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=50,
        help="Max agentic turns per agent call (default: 50)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(verbose=args.verbose)

    repo_path = Path(args.repo).resolve()
    if not (repo_path / ".git").exists():
        print(f"Error: {repo_path} is not a git repository", file=sys.stderr)
        sys.exit(1)

    config = PipelineConfig(
        repo_path=repo_path,
        max_retries=args.max_retries,
        test_command=args.test_command,
        agent=AgentConfig(
            model=args.model,
            max_budget_usd=args.budget,
            max_turns=args.max_turns,
        ),
    )

    result = asyncio.run(run_pipeline(args.task, config))

    # ── Print summary ────────────────────────────────────────────
    print()
    print("=" * 60)
    print(f"  Task: {result.task[:80]}")
    print(f"  Result: {result.subtasks_completed}/{result.subtasks_total} subtasks completed")
    if result.branches_merged:
        print(f"  Merged: {', '.join(result.branches_merged)}")
    if result.branches_rejected:
        print(f"  Rejected: {', '.join(result.branches_rejected)}")
    print(f"  Summary: {result.summary}")
    print("=" * 60)

    sys.exit(0 if result.subtasks_failed == 0 else 1)


if __name__ == "__main__":
    main()
