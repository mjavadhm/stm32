import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class RequestType(StrEnum):
    """Pipeline entry points (End-to-End vs Copilot modes)."""

    full_project = "full_project"  # End-to-End generation
    debug = "debug"                # Copilot: analyze compile/runtime errors
    optimize = "optimize"          # Copilot: RAM/CPU optimization
    test = "test"                  # Copilot: generate/run tests


class RunStatus(StrEnum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class PinSelectionPolicy(StrEnum):
    deterministic = "deterministic"
    explicit = "explicit"
    llm = "llm"


class Project(SQLModel, table=True):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    name: str
    user_request: str
    request_type: RequestType = RequestType.full_project
    status: RunStatus = RunStatus.pending
    # Resolved at creation time. Later global setting changes deliberately do
    # not rewrite existing projects.
    pin_selection_policy: str = PinSelectionPolicy.deterministic.value
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class TaskRun(SQLModel, table=True):
    """One agent execution inside a project pipeline (centralized run log)."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    agent_name: str
    status: RunStatus = RunStatus.pending
    result: str | None = None  # JSON payload produced by the agent
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class AgentSetting(SQLModel, table=True):
    """Per-agent runtime settings, editable from the dashboard later (M7).

    model = None means: fall back to the default LLM_MODEL from .env.
    """

    agent_name: str = Field(primary_key=True)
    model: str | None = None
    enabled: bool = True
    updated_at: datetime = Field(default_factory=utcnow)


class GenerationSetting(SQLModel, table=True):
    """Singleton defaults for deterministic generation policy."""

    id: int = Field(default=1, primary_key=True)
    pin_selection_policy: str = PinSelectionPolicy.deterministic.value
    updated_at: datetime = Field(default_factory=utcnow)
