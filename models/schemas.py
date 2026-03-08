from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class SubtaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class ReviewVerdict(str, Enum):
    APPROVED = "approved"
    NEEDS_CHANGES = "needs_changes"
    REJECTED = "rejected"


class Subtask(BaseModel):
    id: str = Field(description="Unique identifier like 'task-01'")
    title: str = Field(description="Short title for the subtask")
    description: str = Field(description="Detailed description of what to do")
    files_likely_affected: list[str] = Field(
        default_factory=list,
        description="Paths likely to be modified, relative to repo root",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="IDs of subtasks this depends on",
    )
    acceptance_criteria: list[str] = Field(
        description="Specific criteria to verify completion",
    )


class TaskPlan(BaseModel):
    """Structured output from the planning agent."""

    original_task: str = Field(description="The original high-level task description")
    subtasks: list[Subtask] = Field(description="List of subtasks to complete")
    execution_order: list[list[str]] = Field(
        description=(
            "Groups of subtask IDs that can run in parallel. "
            "Each group runs after the previous group completes. "
            "Example: [['task-01', 'task-02'], ['task-03']]"
        ),
    )
    test_command: str = Field(
        default="",
        description="Command to run the full test suite, or empty if unknown",
    )


class SpecialistResult(BaseModel):
    """Structured output from a specialist agent."""

    subtask_id: str = Field(description="The ID of the subtask that was worked on")
    status: SubtaskStatus = Field(description="Final status of the subtask")
    summary: str = Field(description="What was done, in 2-3 sentences")
    files_modified: list[str] = Field(
        default_factory=list,
        description="Paths of files that were modified",
    )
    tests_passed: bool = Field(description="Whether tests passed after changes")
    test_output: str = Field(
        default="",
        description="Relevant test output or error messages",
    )
    error_message: Optional[str] = Field(
        default=None,
        description="Error message if the subtask failed",
    )


class ReviewResult(BaseModel):
    """Structured output from the review agent."""

    subtask_id: str = Field(description="The ID of the subtask being reviewed")
    verdict: ReviewVerdict = Field(description="Review decision")
    issues: list[str] = Field(
        default_factory=list,
        description="Specific issues found in the changes",
    )
    suggestions: list[str] = Field(
        default_factory=list,
        description="Suggestions for improvement",
    )
    reasoning: str = Field(description="Explanation of the review decision")


class SecuritySeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class SecurityVerdict(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class SecurityFinding(BaseModel):
    """A single security finding from the security review."""

    severity: SecuritySeverity = Field(description="Severity level of the finding")
    category: str = Field(
        description="Security category (e.g. 'Injection', 'Broken Access Control')",
    )
    cwe_id: Optional[str] = Field(
        default=None,
        description="CWE identifier if applicable (e.g. 'CWE-79')",
    )
    location: str = Field(
        description="File path and line number (e.g. 'src/auth.py:42')",
    )
    description: str = Field(
        description="Description of the vulnerability or concern",
    )
    recommendation: str = Field(
        description="Specific remediation recommendation",
    )


class SecurityReviewResult(BaseModel):
    """Structured output from the security review agent."""

    subtask_id: str = Field(description="The ID of the subtask being reviewed")
    verdict: SecurityVerdict = Field(description="Security review decision")
    findings: list[SecurityFinding] = Field(
        default_factory=list,
        description="List of security findings",
    )
    summary: str = Field(
        description="Overall security assessment in 2-4 sentences",
    )


class PipelineResult(BaseModel):
    """Final result of the entire pipeline."""

    task: str
    subtasks_total: int
    subtasks_completed: int
    subtasks_failed: int
    branches_merged: list[str] = Field(default_factory=list)
    branches_rejected: list[str] = Field(default_factory=list)
    security_blocked: list[str] = Field(default_factory=list)
    security_findings_summary: list[str] = Field(default_factory=list)
    summary: str
