from typing import TypedDict


class PipelineState(TypedDict, total=False):
    """Shared memory passed between agents in the workflow graph.

    M3+: extend with the structured JSON-schema contracts
    (requirements doc, architecture, pin tables, generated files, ...).
    """

    project_id: str
    user_request: str
    request_type: str

    # Mock agent outputs (M1 only)
    analysis: str
    plan: str
    copilot_result: str
