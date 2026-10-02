"""M4 P5 phase 2: the build node, offline.

The sandbox is an `httpx.MockTransport`, the workspace a temp directory and
the database SQLite, so the node is exercised end to end without a toolchain.
"""

import asyncio
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, select

from app.agents import build as build_module
from app.build import artifacts as artifacts_module
from app.build import workspace
from app.build.client import BuilderClient
from app.core.config import settings
from app.db.models import Artifact, ArtifactKind, Project, RequestType, TaskRun
from app.db.session import upgrade_database
from app.orchestrator.contracts import (
    BUILD_FAILED,
    BUILD_OK,
    BUILD_UNAVAILABLE,
    CubeMXPlan,
    dump,
)
from app.orchestrator.graph import pipeline_for

FIXTURES = Path(__file__).parent / "fixtures"
GCC_LOG = (FIXTURES / "gcc_errors.txt").read_text(encoding="utf-8")
PROJECT_ID = "p1"
PLAN = CubeMXPlan(mcu="STM32F407VGT6")

SIZE_OUTPUT = (
    "   text\t   data\t    bss\t    dec\t    hex\tfilename\n"
    "  12000\t    120\t   2048\t  14168\t   3758\tbuild/app.elf\n"
)
OK_PAYLOAD = {
    "status": "ok",
    "exit_code": 0,
    "duration_ms": 4200,
    "toolchain": "arm-none-eabi-gcc 12.2.1",
    "command": "make -j4",
    "log": "arm-none-eabi-size build/app.elf\n",
    "artifacts": {"elf": "build/app.elf", "bin": "build/app.bin"},
    "size_output": SIZE_OUTPUT,
}


def _failed_payload() -> dict:
    """gcc output as the sandbox would print it for *this* workspace."""
    log = GCC_LOG.replace("/workspaces/7f3a1c", f"{settings.workspace_root}/{PROJECT_ID}")
    return {**OK_PAYLOAD, "status": "failed", "exit_code": 2, "log": log, "artifacts": {}}


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """Workspace with a tiny project, a migrated SQLite DB and a build TaskRun."""
    monkeypatch.setattr(settings, "workspace_root", str(tmp_path / "ws"))
    workspace.ensure_workspace(PROJECT_ID)
    workspace.write_file(PROJECT_ID, "Makefile", "all:\n\techo build\n")
    workspace.write_file(PROJECT_ID, "Core/Src/main.c", "int main(void) { return 0; }\n")
    workspace.write_file(PROJECT_ID, "demo.ioc", "Mcu.Name=STM32F407VGTx\n")

    url = f"sqlite:///{tmp_path / 'build.db'}"
    engine = create_engine(url)
    upgrade_database(engine, url)
    monkeypatch.setattr(artifacts_module, "_get_engine", lambda: engine)
    with Session(engine) as session:
        session.add(Project(id=PROJECT_ID, name="demo", user_request="r"))
        session.commit()
        session.add(TaskRun(project_id=PROJECT_ID, agent_name="build"))
        session.commit()
    return engine


def _client(payload: dict, calls: list | None = None) -> BuilderClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(200, json=payload)

    return BuilderClient(base_url="http://builder:9000", transport=httpx.MockTransport(handler))


def _run(client: BuilderClient, **kwargs):
    async def go():
        try:
            plan = kwargs.pop("plan", PLAN)
            return await build_module.run_build(PROJECT_ID, plan, client=client, **kwargs)
        finally:
            await client.aclose()

    return asyncio.run(go())


def test_full_project_pipeline_ends_with_build():
    assert pipeline_for(RequestType.full_project)[-2:] == ["firmware", "build"]


def test_memory_totals_come_from_the_device_table():
    assert build_module.memory_totals("STM32F407VGT6") == (1024 * 1024, 128 * 1024)
    assert build_module.memory_totals("ATmega328P") == (0, 0)


def test_successful_build_is_sized_and_indexed(env):
    workspace.write_file(PROJECT_ID, "build/app.elf", "pretend-elf")
    calls: list[httpx.Request] = []

    result = _run(_client(OK_PAYLOAD, calls))

    assert result.status == BUILD_OK
    assert len(calls) == 1
    assert b'"clean":true' in calls[0].content.replace(b" ", b"")
    assert result.size.flash_total == 1024 * 1024
    assert result.size.flash_pct == 1.2

    with Session(env) as session:
        build_task = session.exec(select(TaskRun).where(TaskRun.agent_name == "build")).one()
        rows = {a.path: a for a in session.exec(select(Artifact)).all()}
    assert rows["Makefile"].kind == ArtifactKind.source.value
    assert rows["demo.ioc"].kind == ArtifactKind.ioc.value
    assert rows["build/app.elf"].kind == ArtifactKind.binary.value
    assert rows["build/build.log"].kind == ArtifactKind.build_log.value
    # Reported but never written to disk: not indexed.
    assert "build/app.bin" not in rows
    for artifact in rows.values():
        assert artifact.attempt == 1
        assert artifact.sha256
        assert artifact.task_run_id == build_task.id


def test_failed_build_is_a_result_not_an_exception(env, monkeypatch):
    monkeypatch.setattr(build_module, "get_builder_client", lambda: _client(_failed_payload()))

    update = asyncio.run(
        build_module.build_node({"project_id": PROJECT_ID, "cubemx": dump(PLAN)})
    )

    assert update["build"]["status"] == BUILD_FAILED
    summary = update["build_artifacts"]
    assert summary["status"] == BUILD_FAILED
    assert summary["errors"] == 4
    assert "Core/Src/main.c:42" in summary["first_errors"][0]


def test_attempt_is_taken_from_state(env, monkeypatch):
    monkeypatch.setattr(build_module, "get_builder_client", lambda: _client(OK_PAYLOAD))

    update = asyncio.run(
        build_module.build_node({"project_id": PROJECT_ID, "cubemx": dump(PLAN), "attempt": 2})
    )

    assert update["build"]["attempt"] == 2
    with Session(env) as session:
        attempts = {a.attempt for a in session.exec(select(Artifact)).all()}
    assert attempts == {2}


def test_missing_workspace_skips_the_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_root", str(tmp_path / "empty"))
    calls: list[httpx.Request] = []

    result = _run(_client(OK_PAYLOAD, calls))

    assert result.status == BUILD_UNAVAILABLE
    assert calls == []
    assert "no project workspace" in result.log_tail


def test_unknown_mcu_still_builds_without_percentages(env):
    result = _run(_client(OK_PAYLOAD), plan=CubeMXPlan(mcu=""))

    assert result.status == BUILD_OK
    assert result.size.flash_bytes == 12120
    assert result.size.flash_pct == 0.0


def test_rebuilding_the_same_attempt_replaces_its_index(env):
    _run(_client(OK_PAYLOAD))
    _run(_client(OK_PAYLOAD))

    with Session(env) as session:
        paths = [a.path for a in session.exec(select(Artifact)).all()]
    assert len(paths) == len(set(paths))


def test_a_broken_database_does_not_break_the_build(env, monkeypatch):
    def broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(artifacts_module, "_get_engine", broken)

    result = _run(_client(OK_PAYLOAD))

    assert result.status == BUILD_OK
