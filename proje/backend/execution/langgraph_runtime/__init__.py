"""LangGraph-backed execution paths (Phase 2+: Single; Phase 4+: Workforce)."""

from execution.langgraph_runtime.single import run_single_agent
from execution.langgraph_runtime.workforce import run_workforce_in_thread

__all__ = ["run_single_agent", "run_workforce_in_thread"]
