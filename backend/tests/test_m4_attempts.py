"""M4 phase 1: TaskRun attempts, Artifact/AgentCall tables, call telemetry.

All offline: SQLite in tmp_path, no LLM, no PageVault.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from alembic import command
from app.core import usage as usage_module
from app.core.llm import AgentLLM
from app.core.usage import llm_call_scope, record_agent_call
from app.db.models import AgentCall, Artifact, ArtifactKind, Project, RunStatus, TaskRun
from app.db.runs import current_attempt, latest_task, start_new_attempt
from app.db.session import _alembic_config, upgrade_database


def _fresh(tmp_path: Path, name: str = "m4.db"):
    url = f"sqlite:///{tmp_path / name}"
    engine = create_engine(url)
    upgrade_database(engine, url)
    return engine


def _project(session: Session) -> Project:
    project = Project(name="p", user_request="r")
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def test_fresh_database_has_m4_schema(tmp_path: Path):
    engine = _fresh(tmp_path)
    inspector = inspect(engine)

    assert {"artifact", "agentcall"} <= set(inspector.get_table_names())
    columns = {column["name"] for column in inspector.get_columns("taskrun")}
    assert "attempt" in columns
    uniques = {c["name"] for c in inspector.get_unique_constraints("taskrun")}
    assert "uq_taskrun_project_agent_attempt" in uniques
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert revision == "0004_m4_attempts"


def test_duplicate_attempt_is_rejected(tmp_path: Path):
    engine = _fresh(tmp_path)
    with Session(engine) as session:
        project = _project(session)
        session.add(TaskRun(project_id=project.id, agent_name="build", attempt=1))
        session.commit()
        session.add(TaskRun(project_id=project.id, agent_name="build", attempt=1))
        with pytest.raises(IntegrityError):
            session.commit()


def test_upgrade_from_0003_numbers_existing_duplicates(tmp_path: Path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = create_engine(url)
    command.upgrade(_alembic_config(url), "0003_chat")
    now = datetime.now(UTC).replace(tzinfo=None)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO project (id, name, user_request, request_type, status, "
                "pin_selection_policy, created_at, updated_at) VALUES "
                "('p1', 'n', 'r', 'full_project', 'done', 'deterministic', :now, :now)"
            ),
            {"now": now},
        )
        for task_id, agent in (("a", "firmware"), ("b", "firmware"), ("c", "router")):
            connection.execute(
                text(
                    "INSERT INTO taskrun (id, project_id, agent_name, status) "
                    "VALUES (:id, 'p1', :agent, 'done')"
                ),
                {"id": task_id, "agent": agent},
            )

    upgrade_database(engine, url)

    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, attempt FROM taskrun ORDER BY id")
        ).all()
    assert [tuple(row) for row in rows] == [("a", 1), ("b", 2), ("c", 1)]


def test_start_new_attempt_and_latest_task(tmp_path: Path):
    engine = _fresh(tmp_path)
    with Session(engine) as session:
        project = _project(session)
        assert latest_task(session, project.id, "firmware") is None
        assert current_attempt(session, project.id, "firmware") is None

        first = start_new_attempt(session, project.id, "firmware")
        session.commit()
        first.status = RunStatus.failed
        session.add(first)
        second = start_new_attempt(session, project.id, "firmware")
        session.commit()

        assert (first.attempt, second.attempt) == (1, 2)
        latest = latest_task(session, project.id, "firmware")
        assert latest is not None and latest.id == second.id
        assert latest.status == RunStatus.pending
        assert current_attempt(session, project.id, "firmware") == 2


def test_artifact_unique_per_path_and_attempt(tmp_path: Path):
    engine = _fresh(tmp_path)
    with Session(engine) as session:
        project = _project(session)
        for attempt in (1, 2):
            session.add(
                Artifact(
                    project_id=project.id,
                    kind=ArtifactKind.source.value,
                    path="Core/Src/main.c",
                    attempt=attempt,
                )
            )
        session.commit()
        assert len(session.exec(select(Artifact)).all()) == 2

        session.add(Artifact(project_id=project.id, path="Core/Src/main.c", attempt=2))
        with pytest.raises(IntegrityError):
            session.commit()


def test_record_agent_call_is_noop_without_scope(tmp_path: Path, monkeypatch):
    engine = _fresh(tmp_path)
    monkeypatch.setattr(usage_module, "_get_engine", lambda: engine)

    asyncio.run(record_agent_call("router", "m", status="ok", duration_ms=1))

    with Session(engine) as session:
        assert session.exec(select(AgentCall)).all() == []


def test_record_agent_call_never_raises(monkeypatch):
    def broken_engine():
        raise RuntimeError("db down")

    monkeypatch.setattr(usage_module, "_get_engine", broken_engine)

    async def run():
        with llm_call_scope("missing"):
            await record_agent_call("router", "m", status="ok", duration_ms=1)

    asyncio.run(run())  # must not raise


class _FakeCompletions:
    def __init__(self, fail: bool = False):
        self.fail = fail

    async def create(self, **kwargs):
        if self.fail:
            raise RuntimeError("provider exploded")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="hello"))],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=4, total_tokens=15),
        )


def _fake_llm(fail: bool = False) -> AgentLLM:
    client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions(fail)))
    return AgentLLM(
        agent_name="firmware",
        client=client,  # type: ignore[arg-type]
        model="test-model",
    )


def test_agent_llm_chat_records_usage_with_attempt(tmp_path: Path, monkeypatch):
    engine = _fresh(tmp_path)
    monkeypatch.setattr(usage_module, "_get_engine", lambda: engine)
    with Session(engine) as session:
        project = _project(session)
        start_new_attempt(session, project.id, "firmware")
        start_new_attempt(session, project.id, "firmware")
        session.commit()
        project_id = project.id

    async def run():
        with llm_call_scope(project_id):
            return await _fake_llm().chat([{"role": "user", "content": "hi"}])

    assert asyncio.run(run()) == "hello"

    with Session(engine) as session:
        calls = session.exec(select(AgentCall)).all()
    assert len(calls) == 1
    call = calls[0]
    assert (call.project_id, call.agent_name, call.model) == (project_id, "firmware", "test-model")
    assert (call.prompt_tokens, call.completion_tokens, call.total_tokens) == (11, 4, 15)
    assert call.attempt == 2
    assert call.status == "ok"


def test_agent_llm_chat_records_failure_and_reraises(tmp_path: Path, monkeypatch):
    engine = _fresh(tmp_path)
    monkeypatch.setattr(usage_module, "_get_engine", lambda: engine)
    with Session(engine) as session:
        project_id = _project(session).id

    async def run():
        with llm_call_scope(project_id):
            await _fake_llm(fail=True).chat([{"role": "user", "content": "hi"}])

    with pytest.raises(RuntimeError, match="provider exploded"):
        asyncio.run(run())

    with Session(engine) as session:
        call = session.exec(select(AgentCall)).one()
    assert call.status == "error"
    assert "provider exploded" in (call.error or "")
    assert call.attempt is None  # no TaskRun for this agent yet
