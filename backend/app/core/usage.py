"""LLM call telemetry (``AgentCall`` rows).

The worker sets the project scope once per pipeline run with
``llm_call_scope(project_id)``; every ``AgentLLM.chat`` inside that scope is
recorded. Calls outside a scope (chat UI, health check, unit tests) are not
recorded, which keeps this a no-op wherever no project database row exists.

Recording is best-effort: the write runs in a thread so the event loop is
not blocked, and any failure is logged and swallowed.
"""

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

logger = logging.getLogger(__name__)

_project_scope: ContextVar[str | None] = ContextVar("llm_project_scope", default=None)


@contextmanager
def llm_call_scope(project_id: str) -> Iterator[None]:
    """Attribute every LLM call made inside this block to `project_id`.

    ContextVars are copied into tasks created inside the block, so graph
    nodes scheduled by LangGraph inherit the scope.
    """
    token = _project_scope.set(project_id)
    try:
        yield
    finally:
        _project_scope.reset(token)


def current_project_scope() -> str | None:
    return _project_scope.get()


def _get_engine():
    """Indirection so tests can point recording at a temporary database."""
    from app.db.session import engine

    return engine


def _usage_numbers(usage) -> tuple[int | None, int | None, int | None]:
    if usage is None:
        return None, None, None
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    total = getattr(usage, "total_tokens", None)
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    return prompt, completion, total


def _write_call(
    project_id: str,
    agent_name: str,
    model: str,
    status: str,
    usage,
    duration_ms: int,
    error: str | None,
) -> None:
    from sqlmodel import Session

    from app.db.models import AgentCall
    from app.db.runs import current_attempt

    prompt, completion, total = _usage_numbers(usage)
    with Session(_get_engine()) as session:
        session.add(
            AgentCall(
                project_id=project_id,
                agent_name=agent_name,
                attempt=current_attempt(session, project_id, agent_name),
                model=model,
                status=status,
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                duration_ms=duration_ms,
                error=(error or None) and error[:2000],
            )
        )
        session.commit()


async def record_agent_call(
    agent_name: str,
    model: str,
    *,
    status: str,
    usage=None,
    duration_ms: int,
    error: str | None = None,
) -> None:
    """Persist one call if a project scope is active. Never raises."""
    project_id = current_project_scope()
    if project_id is None:
        return
    try:
        await asyncio.to_thread(
            _write_call, project_id, agent_name, model, status, usage, duration_ms, error
        )
    except Exception:
        logger.warning("recording LLM call for %s failed", agent_name, exc_info=True)
