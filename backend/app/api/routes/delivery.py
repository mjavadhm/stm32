"""Delivery API (M4 P6): the generated project as files, a zip and a build.

Everything is read from the project workspace -- the same directory the
builder compiled -- so what the user downloads is what was built. Paths come
from the URL, so each one goes through `workspace.safe_join` like a path a
model wrote would.
"""

import io
import re
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel import Session, select

from app.agents.build import AGENT_NAME as BUILD_AGENT
from app.build import workspace
from app.build.artifacts import classify
from app.build.rebuild import stored_payload
from app.db.models import ArtifactKind, Project, RunStatus, TaskRun
from app.db.runs import latest_task, start_new_attempt
from app.db.session import get_session
from app.orchestrator.contracts import BuildResult, ContractError, parse_stored
from app.workers.celery_app import rebuild_project

router = APIRouter(prefix="/projects", tags=["delivery"])

# The code viewer is for source; a multi-megabyte file is a download.
MAX_VIEW_BYTES = 2 * 1024 * 1024
_ACTIVE = (RunStatus.pending, RunStatus.running)


def _project(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _has_workspace(project_id: str) -> bool:
    try:
        return workspace.exists(project_id)
    except workspace.WorkspaceError:
        return False


def _generated_paths(session: Session, project_id: str) -> set[str]:
    """Files the firmware agent wrote (as opposed to templates and drivers)."""
    bundle = stored_payload(latest_task(session, project_id, "firmware")).get("firmware") or {}
    return {
        source.get("path", "")
        for source in bundle.get("files", [])
        if isinstance(source, dict) and source.get("generated", True)
    }


def _archive_name(project: Project) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", project.name or "").strip("-")
    return slug[:64] or project.id


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


@router.get("/{project_id}/files")
def list_project_files(
    project_id: str,
    include_build: bool = False,
    session: Session = Depends(get_session),
) -> dict:
    """Flat file list; the UI folds it into a tree. Empty until codegen ran."""
    _project(session, project_id)
    if not _has_workspace(project_id):
        return {"project_id": project_id, "files": [], "count": 0}
    generated = _generated_paths(session, project_id)
    files = []
    for path in workspace.list_files(project_id, include_build=include_build):
        target = workspace.safe_join(project_id, path)
        files.append(
            {
                "path": path,
                "kind": classify(path),
                "size_bytes": target.stat().st_size,
                "generated": path in generated,
            }
        )
    return {"project_id": project_id, "files": files, "count": len(files)}


@router.get("/{project_id}/files/{file_path:path}")
def get_project_file(
    project_id: str,
    file_path: str,
    session: Session = Depends(get_session),
) -> Response:
    """One file. Text is returned as UTF-8 text, anything else as bytes."""
    _project(session, project_id)
    try:
        target = workspace.safe_join(project_id, file_path)
    except workspace.WorkspaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if target.stat().st_size > MAX_VIEW_BYTES:
        raise HTTPException(status_code=413, detail="File too large to view; use /download")

    data = target.read_bytes()
    if b"\x00" not in data:
        try:
            return Response(data.decode("utf-8"), media_type="text/plain; charset=utf-8")
        except UnicodeDecodeError:
            pass
    return Response(
        data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{target.name}"'},
    )


@router.get("/{project_id}/download")
def download_project(
    project_id: str,
    include_binaries: bool = False,
    session: Session = Depends(get_session),
) -> Response:
    """The project as a zip that builds with `make` on a clean machine.

    `build/` is left out (it is the builder's output, not source) unless
    `include_binaries` asks for the .elf/.bin/.hex/.map of the last build.
    """
    project = _project(session, project_id)
    if not _has_workspace(project_id):
        raise HTTPException(status_code=404, detail="Project has no files yet")
    paths = workspace.list_files(project_id)
    if include_binaries:
        paths += [
            path
            for path in workspace.list_files(project_id, include_build=True)
            if classify(path) == ArtifactKind.binary.value
        ]
    if not paths:
        raise HTTPException(status_code=404, detail="Project has no files yet")

    folder = _archive_name(project)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(workspace.safe_join(project_id, path), f"{folder}/{path}")
    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{folder}.zip"'},
    )


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


def _build_view(task: TaskRun) -> dict:
    payload = stored_payload(task)
    try:
        result = parse_stored(BuildResult, payload["build"]) if payload.get("build") else None
    except ContractError:
        result = None
    return {
        "attempt": task.attempt,
        "status": task.status,
        "error": task.error,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
        "result": result.model_dump() if result is not None else None,
        "summary": payload.get("build_artifacts"),
    }


@router.get("/{project_id}/build")
def get_project_build(project_id: str, session: Session = Depends(get_session)) -> dict:
    """Latest build (full `BuildResult` with diagnostics) plus every attempt's status."""
    _project(session, project_id)
    tasks = session.exec(
        select(TaskRun)
        .where(TaskRun.project_id == project_id, TaskRun.agent_name == BUILD_AGENT)
        .order_by(TaskRun.attempt.desc())  # type: ignore[attr-defined]
    ).all()
    if not tasks:
        raise HTTPException(status_code=404, detail="Project has no build step")
    history = []
    for task in tasks:
        summary = stored_payload(task).get("build_artifacts") or {}
        history.append(
            {
                "attempt": task.attempt,
                "status": task.status,
                "build_status": summary.get("status"),
                "errors": summary.get("errors"),
                "manual": bool(summary.get("manual")),
            }
        )
    return {"project_id": project_id, "latest": _build_view(tasks[0]), "attempts": history}


@router.post("/{project_id}/build", status_code=202)
def rebuild_project_endpoint(project_id: str, session: Session = Depends(get_session)) -> dict:
    """Compile the workspace again as it is now, as a new build attempt."""
    project = _project(session, project_id)
    if project.status in _ACTIVE:
        raise HTTPException(status_code=409, detail="Pipeline is still running")
    current = latest_task(session, project_id, BUILD_AGENT)
    if current is not None and current.status in _ACTIVE:
        raise HTTPException(status_code=409, detail="A build is already queued or running")
    if not _has_workspace(project_id):
        raise HTTPException(status_code=409, detail="Project has no files to build")

    task = start_new_attempt(session, project_id, BUILD_AGENT)
    session.commit()
    session.refresh(task)
    rebuild_project.delay(project_id, task.attempt)
    return {"project_id": project_id, "attempt": task.attempt, "status": task.status}
