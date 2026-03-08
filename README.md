# Claude Multi-Agent Orchestrator

A multi-agent system that decomposes coding tasks into parallel subtasks, executes them in isolated git worktrees, reviews and security-scans the results, and merges approved changes back to your main branch.

Built with the [Claude Agent SDK](https://pypi.org/project/claude-agent-sdk/) and Python.

## How It Works

The pipeline runs in four phases:

```
Phase A: PLAN
  A planner agent reads your codebase and breaks the task into
  independent subtasks, deciding which can run in parallel.

Phase B: EXECUTE (parallel)
  Each subtask gets its own specialist agent running in an isolated
  git worktree. Independent subtasks run simultaneously.

Phase C: REVIEW + SECURITY SCAN (parallel)
  A code reviewer evaluates each diff for correctness and quality.
  An optional security reviewer scans for vulnerabilities (OWASP Top 10,
  hardcoded secrets, injection flaws, etc.). Both run in parallel.
  If either requests changes, the specialist gets a second chance
  with the feedback.

Phase D: MERGE (sequential)
  Approved branches merge to main one at a time. After each merge,
  your test suite runs. If tests fail, the merge is rolled back.
```

## Agents

| Agent | Role | Tools | Isolation |
|-------|------|-------|-----------|
| **Planner** | Decomposes task into subtasks, determines parallel execution groups | Read, Glob, Grep (read-only) | Repo root |
| **Specialist** | Implements each subtask: writes code, runs tests, commits | Read, Write, Edit, Bash, Glob, Grep | Own git worktree per task |
| **Code Reviewer** | Evaluates diffs for correctness, completeness, quality | Read, Bash, Glob, Grep (read-only) | Repo root |
| **Security Reviewer** | White-hat vulnerability scanner with CWE references | Read, Bash, Glob, Grep (read-only) | Repo root |

### Security Reviewer Details

The security specialist acts as a white-hat auditor, scanning each diff for:

- Injection flaws (SQL injection, XSS, command injection)
- Hardcoded secrets, API keys, tokens in source code
- Authentication and authorization bypass
- Insecure deserialization (eval, exec, pickle, yaml.load)
- Path traversal and SSRF
- Weak cryptography and predictable randomness
- Race conditions and TOCTOU bugs
- Sensitive data in logs and error messages

Findings include severity levels (CRITICAL/HIGH/MEDIUM/LOW/INFO) and CWE identifiers. CRITICAL and HIGH findings block merging. The system is **fail-closed**: if the security agent crashes, it defaults to FAIL rather than silently passing unreviewed code.

## Setup

**Prerequisites:** Python 3.12+, an [Anthropic API key](https://console.anthropic.com/)

```bash
git clone https://github.com/johnnyleland/claude-multi-agent.git
cd claude-multi-agent

# Create virtual environment and install dependencies
uv venv --python 3.12 .venv
source .venv/bin/activate        # Linux/Mac
# .venv\Scripts\activate         # Windows

uv pip install -r requirements.txt

# Set your API key
export ANTHROPIC_API_KEY="sk-ant-..."
```

## Usage

### Basic

```bash
python run.py "Fix the bug in app.py and add tests" --repo /path/to/your/repo
```

### With security review

```bash
python run.py "Add user authentication endpoint" \
  --repo /path/to/your/repo \
  --security-review \
  --test-command "python -m pytest -v"
```

### All options

```bash
python run.py "Your task description" \
  --repo /path/to/repo \         # Target git repository (default: .)
  --test-command "pytest -v" \   # Test command for merge gates
  --security-review \            # Enable vulnerability scanning
  --model sonnet \               # Claude model (default: sonnet)
  --budget 5.0 \                 # Max USD per agent call (default: 5.0)
  --max-turns 50 \               # Max agent tool-use rounds (default: 50)
  --max-retries 2 \              # Retry attempts for failed subtasks (default: 2)
  -v                             # Verbose logging
```

### CLI Reference

| Flag | Default | Description |
|------|---------|-------------|
| `task` | *(required)* | High-level task description in natural language |
| `--repo` | `.` | Path to the target git repository |
| `--test-command` | *(auto-detected)* | Test command used as a merge gate |
| `--security-review` | off | Enable security vulnerability scanning |
| `--model` | `sonnet` | Claude model to use |
| `--budget` | `5.0` | Max USD spend per agent call |
| `--max-turns` | `50` | Max agentic tool-use rounds per agent call |
| `--max-retries` | `2` | Retry/revision attempts per subtask |
| `-v, --verbose` | off | Enable debug logging |

## Example Output

```
============================================================
  Task: Fix bugs in calculator.py and add comprehensive tests
  Result: 4/4 subtasks completed
  Merged: agent/task-01, agent/task-02, agent/task-03, agent/task-04
  Security findings:
    INFO [CWE-209] at calculator.py:37: User input interpolated into error message
  Summary: Completed 4/4 subtasks. Merged: agent/task-01, ...
============================================================
```

## Project Structure

```
.
├── run.py                          # CLI entry point
├── config.py                       # AgentConfig + PipelineConfig
├── requirements.txt                # claude-agent-sdk, pydantic
├── models/
│   └── schemas.py                  # Pydantic models for structured agent output
├── orchestrator/
│   ├── pipeline.py                 # 4-phase pipeline with parallel execution
│   ├── planner.py                  # Task decomposition agent
│   ├── executor.py                 # Specialist agent (code implementation)
│   ├── reviewer.py                 # Code review agent
│   ├── security_reviewer.py        # Security vulnerability scanner
│   └── merger.py                   # Merge logic with test gates
├── prompts/
│   ├── planner.py                  # Planner system prompt
│   ├── specialist.py               # Specialist system prompt
│   ├── reviewer.py                 # Code reviewer system prompt
│   └── security_reviewer.py        # Security reviewer system prompt
├── worktree/
│   └── manager.py                  # Git worktree lifecycle management
└── utils.py                        # Logging helpers
```

## How It Stays Safe

- **Isolated execution**: Each specialist works in its own git worktree on a separate branch. Failures never corrupt your main branch.
- **Test gates**: Nothing merges unless your test suite passes after the merge. Failed merges are automatically rolled back.
- **Code review**: Every change is reviewed for correctness before merging.
- **Security scanning**: Opt-in vulnerability detection with severity-based merge blocking.
- **Fail-closed security**: If the security agent crashes, the subtask is blocked (not silently passed).
- **Automatic cleanup**: Worktrees and branches are cleaned up after every run.
- **Budget controls**: Per-agent spending limits prevent runaway costs.
