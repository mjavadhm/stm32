"""M4 P6: delivery API (files, zip, build) and the manual rebuild, offline."""

import asyncio
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlmodel import Session, select

from app.api.routes import delivery as delivery_module
from app.build import artifacts as artifacts_module
from app.build import rebuild as rebuild_module
from app.build import workspace
from app.build.client import BuilderClient
from app.core.config import settings
from app.db.models import Project, RunStatus, TaskRun
from app.db.session import get_session, upgrade_database
from app.orchestrator.contracts import (
    BUILD_FAILED,
    BUILD_OK,
    BuildResult,
    CubeMXPlan,
    Diagnostic,
    dump,
)

PROJECT_ID = "p1"
APP_PATH = "Core/Src/app.c"


class FakeDelay:
    def __init__(self):
        self.calls: list[tuple] = []

    def delay(self, *args):
        self.calls.append(args)


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_root", str(tmp_path / "ws"))
    url = f"sqlite:///{tmp_path / 'delivery.db'}"
    engine = create_engine(url)
    upgrade_database(engine, url)
    monkeypatch.setattr(artifacts_module, "_get_engine", lambda: engine)
    monkeypatch.setattr(rebuild_module, "_get_engine", lambda: engine)
    queue = FakeDelay()
    monkeypatch.setattr(delivery_module, "rebuild_project", queue)

    with Session(engine) as session:
        session.add(
            Project(id=PROJECT_ID, name="MPU demo!", user_request="r", status=RunStatus.done)
        )
        session.commit()

    app = FastAPI()
    app.include_router(delivery_module.router)

    def _session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = _session
    return {"engine": engine, "client": TestClient(app), "queue": queue}


def _write_project():
    workspace.ensure_workspace(PROJECT_ID)
    workspace.write_file(PROJECT_ID, "Makefile", "all:\n\techo build\n")
    workspace.write_file(PROJECT_ID, APP_PATH, "int app(void) { return 0; }\n")
    workspace.write_file(PROJECT_ID, "demo.ioc", "Mcu.Name=STM32F407VGTx\n")
    workspace.write_file(PROJECT_ID, "build/app.elf", "pretend-elf")
    workspace.write_file(PROJECT_ID, "build/app.o", "object")
    workspace.write_file(PROJECT_ID, "build/build.log", "log")


def _add_task(engine, agent_name: str, attempt: int = 1, status=RunStatus.done, payload=None):
    with Session(engine) as session:
        session.add(
            TaskRun(
                project_id=PROJECT_ID,
                agent_name=agent_name,
                attempt=attempt,
                status=status,
                result=json.dumps(payload) if payload is not None else None,
            )
        )
        session.commit()


def _build_payload(status: str, attempt: int, errors: int = 0) -> dict:
    result = BuildResult(
        status=status,
        attempt=attempt,
        diagnostics=[Diagnostic(file=APP_PATH, line=n + 1, message="boom") for n in range(errors)],
    )
    return {"build": dump(result), "build_artifacts": {"status": status, "errors": errors}}


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


def test_file_list_hides_build_output_and_marks_model_code(env):
    _write_project()
    firmware = {"firmware": {"files": [{"path": APP_PATH, "generated": True}]}}
    _add_task(env["engine"], "firmware", payload=firmware)

    body = env["client"].get(f"/projects/{PROJECT_ID}/files").json()

    files = {f["path"]: f for f in body["files"]}
    assert set(files) == {"Makefile", APP_PATH, "demo.ioc"}
    assert files[APP_PATH]["generated"] is True
    assert files["Makefile"]["generated"] is False
    assert files["demo.ioc"]["kind"] == "ioc"
    assert files["Makefile"]["size_bytes"] > 0

    with_build = env["client"].get(f"/projects/{PROJECT_ID}/files?include_build=true").json()
    kinds = {f["path"]: f["kind"] for f in with_build["files"]}
    assert kinds["build/app.elf"] == "binary"
    assert kinds["build/build.log"] == "build_log"


def test_file_list_is_empty_before_codegen(env):
    body = env["client"].get(f"/projects/{PROJECT_ID}/files").json()

    assert body["files"] == []
    assert env["client"].get("/projects/nope/files").status_code == 404


def test_file_content_is_text_and_binaries_are_bytes(env):
    _write_project()
    workspace.write_file(PROJECT_ID, "build/app.bin", "")
    workspace.safe_join(PROJECT_ID, "build/app.bin").write_bytes(b"\x00\x01\x02")
    client = env["client"]

    text = client.get(f"/projects/{PROJECT_ID}/files/{APP_PATH}")
    assert text.status_code == 200
    assert text.headers["content-type"].startswith("text/plain")
    assert "int app" in text.text

    binary = client.get(f"/projects/{PROJECT_ID}/files/build/app.bin")
    assert binary.headers["content-type"] == "application/octet-stream"
    assert binary.content == b"\x00\x01\x02"

    assert client.get(f"/projects/{PROJECT_ID}/files/Core/Src/missing.c").status_code == 404


def test_file_paths_cannot_leave_the_workspace(env):
    _write_project()
    with Session(env["engine"]) as session:
        with pytest.raises(HTTPException) as caught:
            delivery_module.get_project_file(PROJECT_ID, "../../etc/passwd", session)
    assert caught.value.status_code == 400


def test_download_is_a_buildable_zip_without_build_output(env):
    _write_project()

    response = env["client"].get(f"/projects/{PROJECT_ID}/download")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert 'filename="MPU-demo.zip"' in response.headers["content-disposition"]
    names = set(zipfile.ZipFile(io.BytesIO(response.content)).namelist())
    assert names == {"MPU-demo/Makefile", f"MPU-demo/{APP_PATH}", "MPU-demo/demo.ioc"}


def test_download_can_include_binaries_but_never_objects(env):
    _write_project()

    response = env["client"].get(f"/projects/{PROJECT_ID}/download?include_binaries=true")

    names = set(zipfile.ZipFile(io.BytesIO(response.content)).namelist())
    assert "MPU-demo/build/app.elf" in names
    assert "MPU-demo/build/app.o" not in names
    assert "MPU-demo/build/build.log" not in names


def test_download_without_files_is_404(env):
    assert env["client"].get(f"/projects/{PROJECT_ID}/download").status_code == 404


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


def test_build_view_returns_latest_attempt_and_history(env):
    _add_task(env["engine"], "build", 1, payload=_build_payload(BUILD_FAILED, 1, errors=2))
    _add_task(env["engine"], "build", 2, payload=_build_payload(BUILD_OK, 2))

    body = env["client"].get(f"/projects/{PROJECT_ID}/build").json()

    assert body["latest"]["attempt"] == 2
    assert body["latest"]["result"]["status"] == BUILD_OK
    assert [a["attempt"] for a in body["attempts"]] == [2, 1]
    assert body["attempts"][1]["build_status"] == BUILD_FAILED
    assert body["attempts"][1]["errors"] == 2


def test_failed_build_view_carries_diagnostics(env):
    _add_task(env["engine"], "build", 1, payload=_build_payload(BUILD_FAILED, 1, errors=3))

    latest = env["client"].get(f"/projects/{PROJECT_ID}/build").json()["latest"]

    assert len(latest["result"]["diagnostics"]) == 3
    assert latest["result"]["diagnostics"][0]["file"] == APP_PATH


def test_build_view_without_a_build_step_is_404(env):
    assert env["client"].get(f"/projects/{PROJECT_ID}/build").status_code == 404


def test_rebuild_queues_a_new_attempt(env):
    _write_project()
    _add_task(env["engine"], "build", 1, payload=_build_payload(BUILD_FAILED, 1, errors=1))
    _add_task(env["engine"], "build", 2, payload=_build_payload(BUILD_FAILED, 2, errors=1))

    response = env["client"].post(f"/projects/{PROJECT_ID}/build")

    assert response.status_code == 202
    assert response.json()["attempt"] == 3
    assert env["queue"].calls == [(PROJECT_ID, 3)]
    with Session(env["engine"]) as session:
        row = session.exec(
            select(TaskRun).where(TaskRun.agent_name == "build", TaskRun.attempt == 3)
        ).one()
    assert row.status == RunStatus.pending


def test_rebuild_is_refused_while_something_is_running(env):
    _write_project()
    _add_task(env["engine"], "build", 1, status=RunStatus.running)

    assert env["client"].post(f"/projects/{PROJECT_ID}/build").status_code == 409

    with Session(env["engine"]) as session:
        project = session.get(Project, PROJECT_ID)
        project.status = RunStatus.running
        session.add(project)
        session.commit()
    assert env["client"].post(f"/projects/{PROJECT_ID}/build").status_code == 409
    assert env["queue"].calls == []


def test_rebuild_without_files_is_refused(env):
    assert env["client"].post(f"/projects/{PROJECT_ID}/build").status_code == 409


# --------------------------------------------------------------------------
# The worker side
# --------------------------------------------------------------------------


def test_manual_rebuild_fills_its_attempt_row(env):
    _write_project()
    _add_task(env["engine"], "cubemx", payload={"cubemx": dump(CubeMXPlan(mcu="STM32F407VGT6"))})
    _add_task(env["engine"], "build", 2, status=RunStatus.pending)
    payload = {
        "status": "ok",
        "exit_code": 0,
        "duration_ms": 900,
        "toolchain": "arm-none-eabi-gcc 12.2.1",
        "command": "make -j4",
        "log": "done\n",
        "artifacts": {"elf": "build/app.elf"},
        "size_output": (
            "   text\t   data\t    bss\t    dec\t    hex\tfilename\n"
            "  12000\t    120\t   2048\t  14168\t   3758\tbuild/app.elf\n"
        ),
    }
    client = BuilderClient(
        base_url="http://builder:9000",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )

    async def run() -> str:
        try:
            return await rebuild_module.rebuild(PROJECT_ID, 2, client=client)
        finally:
            await client.aclose()

    assert asyncio.run(run()) == BUILD_OK
    with Session(env["engine"]) as session:
        row = session.exec(
            select(TaskRun).where(TaskRun.agent_name == "build", TaskRun.attempt == 2)
        ).one()
    stored = json.loads(row.result)
    assert row.status == RunStatus.done
    assert stored["build"]["attempt"] == 2
    assert stored["build"]["size"]["flash_total"] == 1024 * 1024
    assert stored["build_artifacts"]["manual"] is True


def test_manual_rebuild_of_an_unknown_attempt_is_a_no_op(env):
    assert asyncio.run(rebuild_module.rebuild(PROJECT_ID, 9)) == "not-found"
