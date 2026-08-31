from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.api.routes.generation import effective_generation_settings
from app.db.models import PinSelectionPolicy, Project, RunStatus, TaskRun, utcnow
from app.db.session import get_session
from app.orchestrator.graph import agent_sequence_for
from app.workers.celery_app import run_pipeline

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    request: str = Field(min_length=1)
    pin_selection_policy: PinSelectionPolicy | None = None


def _project_summary(project: Project) -> dict:
    return {
        "id": project.id,
        "name": project.name,
        "request_type": project.request_type,
        "status": project.status,
        "pin_selection_policy": project.pin_selection_policy,
        "error": project.error,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


@router.post("", status_code=201)
def create_project(
    payload: ProjectCreate,
    session: Session = Depends(get_session),
) -> dict:
    """Create a project and enqueue the pipeline.

    Routing used to happen here, which meant a slow or unreachable LLM held
    the user's HTTP request open. Since M3 the router is the graph's first
    node, so this handler only persists the request and returns; the request
    type is filled in by the worker moments later.

    Only the router task is pre-created, because the remaining agents are not
    known until the router has run.
    """
    policy = (
        payload.pin_selection_policy.value
        if payload.pin_selection_policy is not None
        else effective_generation_settings(session).pin_selection_policy
    )
    project = Project(
        name=payload.name,
        user_request=payload.request,
        pin_selection_policy=policy,
    )
    session.add(project)
    session.commit()
    session.refresh(project)

    for agent_name in agent_sequence_for():
        session.add(TaskRun(project_id=project.id, agent_name=agent_name))
    session.commit()

    run_pipeline.delay(project.id)
    return _project_summary(project)


@router.get("")
def list_projects(session: Session = Depends(get_session)) -> list[dict]:
    projects = session.exec(
        select(Project).order_by(Project.created_at.desc())  # type: ignore[union-attr]
    ).all()
    return [_project_summary(p) for p in projects]


@router.get("/{project_id}")
def get_project(project_id: str, session: Session = Depends(get_session)) -> dict:
    """Live progress: project status + per-agent task states."""
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    tasks = session.exec(
        select(TaskRun).where(TaskRun.project_id == project_id)
    ).all()
    return {
        **_project_summary(project),
        "user_request": project.user_request,
        "tasks": [
            {
                "agent_name": t.agent_name,
                "status": t.status,
                "result": t.result,
                "error": t.error,
                "started_at": t.started_at,
                "finished_at": t.finished_at,
            }
            for t in tasks
        ],
    }


@router.post("/{project_id}/cancel")
def cancel_project(project_id: str, session: Session = Depends(get_session)) -> dict:
    """Request cancellation. Takes effect between agent executions."""
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status in (RunStatus.done, RunStatus.failed, RunStatus.cancelled):
        raise HTTPException(
            status_code=409, detail=f"Project already {project.status}"
        )
    project.status = RunStatus.cancelled
    project.updated_at = utcnow()
    session.add(project)
    session.commit()
    return _project_summary(project)
