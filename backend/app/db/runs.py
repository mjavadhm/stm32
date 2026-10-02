"""TaskRun helpers shared by the worker, the API and (P5) the repair loop.

Since M4 a project can hold several rows for one agent, one per attempt.
Everything that used to look a task up by (project_id, agent_name) must go
through `latest_task`, otherwise it would update an arbitrary attempt.
"""

from sqlmodel import Session, select

from app.db.models import RunStatus, TaskRun


def latest_task(session: Session, project_id: str, agent_name: str) -> TaskRun | None:
    """The current (highest-attempt) run of an agent, or None."""
    return session.exec(
        select(TaskRun)
        .where(TaskRun.project_id == project_id, TaskRun.agent_name == agent_name)
        .order_by(TaskRun.attempt.desc())  # type: ignore[attr-defined]
    ).first()


def start_new_attempt(session: Session, project_id: str, agent_name: str) -> TaskRun:
    """Add a fresh pending row for the next attempt of an agent.

    Attempt 1 when the agent has never run in this project. The caller
    commits; the unique constraint turns a concurrent double-start into an
    IntegrityError instead of two rows claiming the same attempt.
    """
    current = latest_task(session, project_id, agent_name)
    task = TaskRun(
        project_id=project_id,
        agent_name=agent_name,
        attempt=(current.attempt + 1) if current is not None else 1,
        status=RunStatus.pending,
    )
    session.add(task)
    return task


def current_attempt(session: Session, project_id: str, agent_name: str) -> int | None:
    task = latest_task(session, project_id, agent_name)
    return task.attempt if task is not None else None
