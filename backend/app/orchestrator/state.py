"""Shared memory passed between agents in the workflow graph.

The design contracts (M3) are stored as plain dicts rather than Pydantic
objects: LangGraph serialises state between nodes, and the worker dumps each
node's update straight into `TaskRun.result` as JSON. Agents rebuild the typed
model on entry with `Requirements.model_validate(...)`, so the contract is
still enforced -- just at the boundary instead of inside the channel.
"""

from typing import Any, TypedDict


class PipelineState(TypedDict, total=False):
    # --- set by the API before the graph starts ---
    project_id: str
    project_name: str
    user_request: str
    pin_selection_policy: str

    # --- router node (M3) ---
    request_type: str
    routing: dict[str, Any]

    # --- design agents (M3), each a serialised contract ---
    requirements: dict[str, Any]
    hardware: dict[str, Any]
    architecture: dict[str, Any]
    cubemx: dict[str, Any]
    cubemx_artifacts: dict[str, Any]

    # --- copilot path (still mocked until M5) ---
    copilot_result: str
