"""M1 workflow graph with conditional entry points.

- full_project requests enter the End-to-End path (analyzer -> planner)
- debug/optimize/test requests enter the Copilot path (copilot node)

Mock nodes are replaced by real agents from M3 onward — the surrounding
machinery (DB state tracking, Celery execution, progress API) stays the same.
"""

from langgraph.graph import END, StateGraph

from app.agents.mock import mock_analyzer, mock_copilot, mock_planner
from app.db.models import RequestType
from app.orchestrator.state import PipelineState

FULL_PROJECT_SEQUENCE: list[str] = ["mock_analyzer", "mock_planner"]
COPILOT_SEQUENCE: list[str] = ["mock_copilot"]


def agent_sequence_for(request_type: RequestType | str) -> list[str]:
    """Agents that will run for a request type. The projects API pre-creates
    one TaskRun per entry so progress is visible before execution starts."""
    value = (
        request_type.value
        if isinstance(request_type, RequestType)
        else str(request_type)
    )
    if value == RequestType.full_project.value:
        return FULL_PROJECT_SEQUENCE
    return COPILOT_SEQUENCE


def _route_entry(state: PipelineState) -> str:
    request_type = state.get("request_type", RequestType.full_project.value)
    if request_type == RequestType.full_project.value:
        return "mock_analyzer"
    return "mock_copilot"


def build_graph():
    graph = StateGraph(PipelineState)
    graph.add_node("mock_analyzer", mock_analyzer)
    graph.add_node("mock_planner", mock_planner)
    graph.add_node("mock_copilot", mock_copilot)

    graph.set_conditional_entry_point(
        _route_entry,
        {"mock_analyzer": "mock_analyzer", "mock_copilot": "mock_copilot"},
    )
    graph.add_edge("mock_analyzer", "mock_planner")
    graph.add_edge("mock_planner", END)
    graph.add_edge("mock_copilot", END)
    return graph.compile()
