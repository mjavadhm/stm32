import asyncio
import json
from datetime import UTC, datetime

from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "stm32ai",
    broker=settings.redis_url,
    backend=settings.redis_url,
)
celery_app.conf.task_track_started = True


def _utcnow() -> datetime:
    return datetime.now(UTC)


@celery_app.task(name="ping")
def ping() -> str:
    """Smoke-test task."""
    return "pong"


@celery_app.task(name="run_pipeline")
def run_pipeline(project_id: str) -> str:
    """Execute the workflow graph for a project, updating DB state live.

    Celery tasks are synchronous, but the graph is driven asynchronously so
    that agents can be `async def` from M3 onward (LangGraph only awaits
    coroutine nodes on the astream/ainvoke path) and so that an agent can
    issue several knowledge-base lookups concurrently.
    """
    return asyncio.run(_run_pipeline_and_cleanup(project_id))


async def _run_pipeline_and_cleanup(project_id: str) -> str:
    """Run the pipeline, then dispose every async client this loop created.

    The OpenAI and PageVault clients are cached singletons, while each task
    gets its own event loop. Reusing a connection pool across loops raises
    "Event loop is closed" on the second task of a worker process, so the
    clients are torn down when the loop that opened them ends.
    """
    from app.core.llm import aclose_llm_clients
    from app.rag import close_rag_client

    try:
        return await _run_pipeline(project_id)
    finally:
        await close_rag_client()
        await aclose_llm_clients()


async def _run_pipeline(project_id: str) -> str:
    from sqlmodel import Session, select

    from app.db.models import Project, RunStatus, TaskRun
    from app.db.session import engine
    from app.orchestrator.graph import (
        ROUTER_NODE,
        agent_sequence_for,
        build_graph,
        pipeline_for,
    )

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

    def _close_unfinished(session: Session, status: RunStatus) -> None:
        """Resolve tasks that will now never run.

        Without this, a cancelled or failed pipeline leaves its remaining
        agents sitting at `pending` forever and the dashboard implies work
        is still queued.
        """
        tasks = session.exec(
            select(TaskRun).where(TaskRun.project_id == project_id)
        ).all()
        for task in tasks:
            if task.status in (RunStatus.pending, RunStatus.running):
                task.status = status
                task.finished_at = _utcnow()
                session.add(task)

    def _fail_project(exc: Exception, failed_agent: str | None) -> None:
        with Session(engine) as session:
            if failed_agent is not None:
                _set_task(
                    session,
                    failed_agent,
                    status=RunStatus.failed,
                    error=str(exc),
                    finished_at=_utcnow(),
                )
            _close_unfinished(session, RunStatus.cancelled)
            project = session.get(Project, project_id)
            if project is not None:
                project.status = RunStatus.failed
                project.error = str(exc)
                project.updated_at = _utcnow()
                session.add(project)
            session.commit()

    # --- mark project as running ---
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if project is None:
            return "not-found"
        if project.status == RunStatus.cancelled:
            _close_unfinished(session, RunStatus.cancelled)
            session.commit()
            return "cancelled"
        project.status = RunStatus.running
        project.updated_at = _utcnow()
        session.add(project)
        session.commit()
        user_request = project.user_request
        project_name = project.name
        pin_selection_policy = project.pin_selection_policy

    # Only the router is known up front -- it is what decides the request
    # type, and therefore which agents run after it. The sequence is extended
    # below as soon as the router reports back.
    sequence = agent_sequence_for()

    state = {
        "project_id": project_id,
        "project_name": project_name,
        "user_request": user_request,
        "pin_selection_policy": pin_selection_policy,
    }

    # The agent currently believed to be executing. `stream_mode="updates"`
    # only reports a node *after* it finishes, so the running marker is
    # driven from the known sequence: mark the first agent running up front,
    # then mark the next one as each completes. This is also what makes
    # error attribution possible -- when the graph raises, this is the node
    # that raised.
    position = 0
    current_agent: str | None = sequence[0] if sequence else None

    if current_agent is not None:
        with Session(engine) as session:
            _set_task(
                session,
                current_agent,
                status=RunStatus.running,
                started_at=_utcnow(),
            )
            session.commit()

    try:
        graph = build_graph()
        async for event in graph.astream(state, stream_mode="updates"):
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
                        _close_unfinished(session, RunStatus.cancelled)
                        session.commit()
                        return "cancelled"

                    _set_task(
                        session,
                        node_name,
                        status=RunStatus.done,
                        result=json.dumps(update, ensure_ascii=False, default=str),
                        finished_at=_utcnow(),
                    )

                    # The router just told us which pipeline this is: persist
                    # the decision and materialise the remaining progress rows
                    # so the dashboard shows the real agent list from here on.
                    if node_name == ROUTER_NODE and update.get("request_type"):
                        request_type_value = update["request_type"]
                        sequence = [ROUTER_NODE, *pipeline_for(request_type_value)]
                        project = session.get(Project, project_id)
                        if project is not None:
                            project.request_type = request_type_value
                            project.updated_at = _utcnow()
                            session.add(project)
                        existing = {
                            task.agent_name
                            for task in session.exec(
                                select(TaskRun).where(
                                    TaskRun.project_id == project_id
                                )
                            ).all()
                        }
                        for agent_name in sequence:
                            if agent_name not in existing:
                                session.add(
                                    TaskRun(
                                        project_id=project_id,
                                        agent_name=agent_name,
                                    )
                                )

                    # Advance the running marker to whatever comes next.
                    if node_name in sequence:
                        position = sequence.index(node_name) + 1
                    else:
                        position += 1
                    current_agent = (
                        sequence[position] if position < len(sequence) else None
                    )
                    if current_agent is not None:
                        _set_task(
                            session,
                            current_agent,
                            status=RunStatus.running,
                            started_at=_utcnow(),
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
        _fail_project(exc, current_agent)
        return "failed"
