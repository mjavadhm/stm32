import json
from datetime import datetime, timezone

from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "stm32ai",
    broker=settings.redis_url,
    backend=settings.redis_url,
)
celery_app.conf.task_track_started = True


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@celery_app.task(name="ping")
def ping() -> str:
    """Smoke-test task."""
    return "pong"


@celery_app.task(name="run_pipeline")
def run_pipeline(project_id: str) -> str:
    """Execute the workflow graph for a project, updating DB state live.

    Cancellation is cooperative: the project status is re-checked after each
    agent finishes, so a cancel request takes effect between nodes.
    """
    from sqlmodel import Session, select

    from app.db.models import Project, RunStatus, TaskRun
    from app.db.session import engine
    from app.orchestrator.graph import build_graph

    # --- mark project as running ---
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if project is None:
            return "not-found"
        if project.status == RunStatus.cancelled:
            return "cancelled"
        project.status = RunStatus.running
        project.updated_at = _utcnow()
        session.add(project)
        session.commit()
        user_request = project.user_request
        request_type = project.request_type

    graph = build_graph()
    state = {
        "project_id": project_id,
        "user_request": user_request,
        "request_type": (
            request_type.value
            if hasattr(request_type, "value")
            else str(request_type)
        ),
    }

    def _set_task(session: Session, agent_name: str, **updates) -> None:
        task = session.exec(
            select(TaskRun).where(
                TaskRun.project_id == project_id,
                TaskRun.agent_name == agent_name,
            )
        ).first()
        if task is None:
            task = TaskRun(project_id=project_id, agent_name=agent_name)
        for key, value in updates.items():
            setattr(task, key, value)
        session.add(task)

    try:
        # stream(..., stream_mode="updates") yields {node_name: partial_state}
        # after each node finishes — that is our live progress hook.
        for event in graph.stream(state, stream_mode="updates"):
            for node_name, update in event.items():
                with Session(engine) as session:
                    project = session.get(Project, project_id)
                    if project is not None and project.status == RunStatus.cancelled:
                        _set_task(
                            session,
                            node_name,
                            status=RunStatus.cancelled,
                            finished_at=_utcnow(),
                        )
                        session.commit()
                        return "cancelled"
                    _set_task(
                        session,
                        node_name,
                        status=RunStatus.done,
                        result=json.dumps(update, ensure_ascii=False, default=str),
                        finished_at=_utcnow(),
                    )
                    session.commit()

        with Session(engine) as session:
            project = session.get(Project, project_id)
            if project is not None and project.status != RunStatus.cancelled:
                project.status = RunStatus.done
                project.updated_at = _utcnow()
                session.add(project)
                session.commit()
        return "done"

    except Exception as exc:
        with Session(engine) as session:
            project = session.get(Project, project_id)
            if project is not None:
                project.status = RunStatus.failed
                project.error = str(exc)
                project.updated_at = _utcnow()
                session.add(project)
                session.commit()
        return "failed"
