"""Workflow graph (M3).

The router is the first node, not a step hidden inside the API handler. Every
run therefore starts the same way, and the routing decision is visible in the
progress view like any other agent.

    router ─┬─ full_project ─> requirements -> datasheet -> architecture -> END
            └─ debug/optimize/test ─> mock_copilot -> END

`_PIPELINES` is the single source of truth for both the graph edges and the
progress rows the worker creates. The previous version encoded the same
routing decision twice -- once for the edges and once for the task list -- and
those two copies were one edit away from disagreeing.
"""

from langgraph.graph import END, StateGraph

from app.agents.architecture import architecture_node
from app.agents.datasheet import datasheet_node
from app.agents.mock import mock_copilot
from app.agents.requirements import requirements_node
from app.agents.router import router_node
from app.db.models import RequestType
from app.orchestrator.state import PipelineState

ROUTER_NODE = "router"

# request_type -> the agents that run after the router, in order.
_PIPELINES: dict[str, list[str]] = {
    RequestType.full_project.value: ["requirements", "datasheet", "architecture"],
    RequestType.debug.value: ["mock_copilot"],
    RequestType.optimize.value: ["mock_copilot"],
    RequestType.test.value: ["mock_copilot"],
}

_NODES = {
    ROUTER_NODE: router_node,
    "requirements": requirements_node,
    "datasheet": datasheet_node,
    "architecture": architecture_node,
    "mock_copilot": mock_copilot,  # replaced by real agents in M5
}


def _value(request_type: RequestType | str) -> str:
    return (
        request_type.value
        if isinstance(request_type, RequestType)
        else str(request_type)
    )


def pipeline_for(request_type: RequestType | str) -> list[str]:
    """Agents that run after the router for a request type."""
    return list(
        _PIPELINES.get(_value(request_type), _PIPELINES[RequestType.debug.value])
    )


def agent_sequence_for(request_type: RequestType | str | None = None) -> list[str]:
    """Full expected sequence, including the router.

    Called with no argument before routing has happened: at enqueue time the
    request type is genuinely unknown, so only the router row can be created
    and the worker fills in the rest once the router has decided.
    """
    if request_type is None:
        return [ROUTER_NODE]
    return [ROUTER_NODE, *pipeline_for(request_type)]


def _route_after_router(state: PipelineState) -> str:
    return pipeline_for(state.get("request_type", RequestType.full_project.value))[0]


def build_graph():
    graph = StateGraph(PipelineState)
    for name, node in _NODES.items():
        graph.add_node(name, node)

    graph.set_entry_point(ROUTER_NODE)

    # One branch per distinct pipeline head.
    heads = {pipeline[0] for pipeline in _PIPELINES.values()}
    graph.add_conditional_edges(
        ROUTER_NODE, _route_after_router, {head: head for head in heads}
    )

    # Chain each pipeline, then terminate it.
    linked: set[tuple[str, str]] = set()
    for pipeline in _PIPELINES.values():
        for current, following in zip(pipeline, pipeline[1:], strict=False):
            if (current, following) not in linked:
                graph.add_edge(current, following)
                linked.add((current, following))
        if (pipeline[-1], END) not in linked:
            graph.add_edge(pipeline[-1], END)
            linked.add((pipeline[-1], END))

    return graph.compile()
