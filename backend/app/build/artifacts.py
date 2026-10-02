"""Index what a build attempt left in the workspace (``Artifact`` rows).

The files themselves stay on disk; this records which ones exist after each
attempt, with a hash, so the delivery API can list them without walking the
filesystem and the repair loop can tell which files an attempt changed.

Recording is best-effort, like LLM telemetry: a database hiccup must never
turn a compiled project into a failed run.
"""

import asyncio
import hashlib
import logging

from app.build import workspace
from app.db.models import ArtifactKind
from app.orchestrator.contracts import BuildResult

logger = logging.getLogger(__name__)

BUILD_LOG_PATH = f"{workspace.BUILD_DIR}/build.log"
_BINARY_SUFFIXES = (".elf", ".bin", ".hex", ".map")


def _get_engine():
    """Indirection so tests can point recording at a temporary database."""
    from app.db.session import engine

    return engine


def classify(path: str) -> str:
    """Artifact kind from a workspace-relative path."""
    if path == BUILD_LOG_PATH:
        return ArtifactKind.build_log.value
    if path.endswith(".ioc"):
        return ArtifactKind.ioc.value
    if path.endswith(".zip"):
        return ArtifactKind.archive.value
    if path.startswith(f"{workspace.BUILD_DIR}/") and path.endswith(_BINARY_SUFFIXES):
        return ArtifactKind.binary.value
    return ArtifactKind.source.value


def collect(project_id: str, result: BuildResult) -> list[tuple[str, str, str, int]]:
    """(path, kind, sha256, size) for every file worth indexing.

    Project source is everything outside `build/`; from `build/` only the
    artifacts the sandbox reported plus the build log are kept -- object
    files are noise. Reported artifacts that are not on disk are skipped.
    """
    paths = list(workspace.list_files(project_id))
    extra = [*result.artifacts.values(), BUILD_LOG_PATH]
    for relative in extra:
        if relative and relative not in paths:
            paths.append(relative)

    rows: list[tuple[str, str, str, int]] = []
    for relative in paths:
        try:
            data = workspace.read_bytes(project_id, relative)
        except workspace.WorkspaceError:
            continue
        rows.append((relative, classify(relative), hashlib.sha256(data).hexdigest(), len(data)))
    return rows


def _write(project_id: str, attempt: int, rows: list[tuple[str, str, str, int]]) -> int:
    from sqlalchemy import delete
    from sqlmodel import Session

    from app.db.models import Artifact
    from app.db.runs import latest_task

    with Session(_get_engine()) as session:
        task = latest_task(session, project_id, "build")
        # A manual rebuild of the same attempt replaces its index instead of
        # tripping the (project_id, path, attempt) constraint.
        session.execute(
            delete(Artifact).where(
                Artifact.project_id == project_id,
                Artifact.attempt == attempt,
            )
        )
        for path, kind, sha256, size in rows:
            session.add(
                Artifact(
                    project_id=project_id,
                    task_run_id=task.id if task is not None else None,
                    kind=kind,
                    path=path,
                    attempt=attempt,
                    sha256=sha256,
                    size_bytes=size,
                )
            )
        session.commit()
    return len(rows)


async def record_artifacts(project_id: str, attempt: int, result: BuildResult) -> int:
    """Index the workspace after one build. Returns rows written; never raises."""
    try:
        rows = collect(project_id, result)
        return await asyncio.to_thread(_write, project_id, attempt, rows)
    except Exception:
        logger.warning("recording artifacts for %s failed", project_id, exc_info=True)
        return 0
