from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.agents.router import classify_request
from app.db.models import Project, RunStatus, TaskRun, utcnow
from app.db.session import get_session
from app.orchestrator.graph import agent_sequence_for
from app.workers.celery_app import run_pipeline

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    request: str = Field(min_length=1)


def _project_summary(project: Project) -> dict:
    return {
        "id": project.id,
        "name": project.name,
        "request_type": project.request_type,
        "status": project.status,
        "error": project.error,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


@router.post("", status_code=201)
async def create_project(
    payload: ProjectCreate,
    session: Session = Depends(get_session),
) -> dict:
    """Create a project, route the request (LLM router), and enqueue the pipeline."""
    request_type = await classify_request(payload.request)

    project = Project(
        name=payload.name,
        user_request=payload.request,
        request_type=request_type,
    )
    session.add(project)
    session.commit()
    session.refresh(project)

    # Pre-create pending TaskRuns (per entry path) so progress is visible
    # immediately.
    for agent_name in agent_sequence_for(project.request_type):
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
