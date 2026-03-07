from __future__ import annotations

import logging
import sys

from claude_agent_sdk import ResultMessage


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stderr)


def extract_result(message: object) -> ResultMessage | None:
    """Return the message if it's a ResultMessage, else None."""
    if isinstance(message, ResultMessage):
        return message
    return None


def format_cost(result: ResultMessage) -> str:
    if result.total_cost_usd is not None:
        return f"${result.total_cost_usd:.4f}"
    return "N/A"


def format_usage(result: ResultMessage) -> str:
    parts = [
        f"turns={result.num_turns}",
        f"cost={format_cost(result)}",
        f"duration={result.duration_ms / 1000:.1f}s",
    ]
    return ", ".join(parts)
