"""LangSmith tracing: every message's path through Hushbell, step by step.

Turn it on with LANGSMITH_TRACING=true and LANGSMITH_API_KEY (and LANGSMITH_PROJECT, which
defaults to "hushbell"). The LangGraph flow is traced on its own; this adds the Claude calls,
the cloud phone tasks and the web lookups as named steps inside it. Off by default.
"""

from __future__ import annotations

import os
from typing import Any, Callable

os.environ.setdefault("LANGSMITH_PROJECT", "hushbell")


def enabled() -> bool:
    return (os.environ.get("LANGSMITH_TRACING", "").strip().lower() in {"1", "true", "yes", "on"}
            and bool(os.environ.get("LANGSMITH_API_KEY")))


def traced(name: str, fn: Callable[..., Any], *args, run_type: str = "chain", **kwargs) -> Any:
    """Call fn(*args, **kwargs), recorded in LangSmith as `name` when tracing is on."""
    if not enabled():
        return fn(*args, **kwargs)
    from langsmith import traceable

    return traceable(name=name, run_type=run_type)(fn)(*args, **kwargs)


def project_url() -> str:
    """Where to see the traces (the LangSmith projects page)."""
    return "https://smith.langchain.com/" if enabled() else ""
