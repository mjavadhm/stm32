import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RequestType(str, Enum):
    """Pipeline entry points (End-to-End vs Copilot modes)."""

    full_project = "full_project"  # End-to-End generation
    debug = "debug"                # Copilot: analyze compile/runtime errors
    optimize = "optimize"          # Copilot: RAM/CPU optimization
    test = "test"                  # Copilot: generate/run tests


class RunStatus(str, Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class Project(SQLModel, table=True):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    name: str
    user_request: str
    request_type: RequestType = RequestType.full_project
    status: RunStatus = RunStatus.pending
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
