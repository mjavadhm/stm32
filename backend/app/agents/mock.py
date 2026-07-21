"""Mock agents for M1.

They simulate work so the orchestrator, DB state tracking, and progress API
can be validated end-to-end. Replaced by real agents from M3 onward.
"""

import time

from app.orchestrator.state import PipelineState


def mock_analyzer(state: PipelineState) -> dict:
    """Pretends to analyze the user request (stands in for M3 agents)."""
    time.sleep(2)
    return {"analysis": f"analyzed request: {state['user_request'][:120]}"}


def mock_planner(state: PipelineState) -> dict:
    """Pretends to produce a build plan (stands in for M4 agents)."""
    time.sleep(2)
    return {"plan": "mock 3-step plan derived from analysis"}


def mock_copilot(state: PipelineState) -> dict:
    """Pretends to handle a Copilot request on existing user code
    (stands in for the M5 debug/optimization/test agents)."""
    time.sleep(2)
    return {
        "copilot_result": (
            f"mock {state.get('request_type', 'unknown')} analysis of user code"
        )
    }
