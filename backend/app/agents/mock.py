"""Remaining mock agent.

The M1 analyzer/planner stubs are gone -- the full-project path now runs the
real requirements, datasheet and architecture agents. Only the Copilot branch
is still simulated; it becomes the debug/optimize/test agents in M5.
"""

import time

from app.orchestrator.state import PipelineState


def mock_copilot(state: PipelineState) -> dict:
    """Stands in for the M5 debug/optimization/test agents."""
    time.sleep(1)
    result = f"mock {state.get('request_type', 'unknown')} analysis of user code"
    return {"copilot_result": result}
