"""Manual rebuild (M4 P6): compile a delivered project again, outside the graph.

`POST /projects/{id}/build` creates the next `build` TaskRun attempt and a
worker runs this. Nothing is regenerated: the workspace is compiled as it is
now, so a user who edited a file (or fixed a template) sees the result of
exactly that change.
"""

import json
import logging
from typing import Any

from sqlmodel import Session, select

from app.agents.build import AGENT_NAME, run_build, summarise_result
from app.build.client import BuilderClient
from app.db.models import RunStatus, TaskRun, utcnow
from app.db.runs import latest_task
from app.orchestrator.contracts import ContractError, CubeMXPlan, dump, parse_stored

logger = logging.getLogger(__name__)


def _get_engine():
    """Indirection so tests can point the rebuild at a temporary database."""
    from app.db.session import engine

    return engine


def stored_payload(task: TaskRun | None) -> dict[str, Any]:
    """The JSON a node wrote into `TaskRun.result`, or {}."""
    if task is None or not task.result:
        return {}
    try:
        payload = json.loads(task.result)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def stored_plan(session: Session, project_id: str) -> CubeMXPlan:
    """The CubeMX plan of the last run; only the MCU (memory totals) matters here."""
    payload = stored_payload(latest_task(session, project_id, "cubemx"))
    try:
        return parse_stored(CubeMXPlan, payload.get("cubemx"))
    except ContractError:
        return CubeMXPlan()


def _attempt_row(session: Session, project_id: str, attempt: int) -> TaskRun | None:
    return session.exec(
        select(TaskRun).where(
            TaskRun.project_id == project_id,
            TaskRun.agent_name == AGENT_NAME,
            TaskRun.attempt == attempt,
        )
    ).first()


async def rebuild(project_id: str, attempt: int, *, client: BuilderClient | None = None) -> str:
    """Run one manual build into the given attempt row. Returns the build status."""
    engine = _get_engine()
    with Session(engine) as session:
        task = _attempt_row(session, project_id, attempt)
        if task is None:
            return "not-found"
        task.status = RunStatus.running
        task.started_at = utcnow()
        session.add(task)
        session.commit()
        plan = stored_plan(session, project_id)

    try:
        result = await run_build(project_id, plan, attempt=attempt, client=client)
    except Exception as exc:  # noqa: BLE001 - run_build should not raise; record if it does
        logger.exception("manual rebuild of %s failed", project_id)
        with Session(engine) as session:
            task = _attempt_row(session, project_id, attempt)
            if task is not None:
                task.status = RunStatus.failed
                task.error = str(exc)
                task.finished_at = utcnow()
                session.add(task)
                session.commit()
        return "error"

    update = {
        "build": dump(result),
        "build_artifacts": {**summarise_result(result), "manual": True},
    }
    with Session(engine) as session:
        task = _attempt_row(session, project_id, attempt)
        if task is not None:
            task.status = RunStatus.done
            task.result = json.dumps(update, ensure_ascii=False, default=str)
            task.finished_at = utcnow()
            session.add(task)
            session.commit()
    return result.status
