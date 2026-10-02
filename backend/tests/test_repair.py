"""M4 P5 phase 3: the bounded repair loop, offline.

The model is a canned reply, the sandbox an `httpx.MockTransport` and the
workspace a temp directory, so the loop runs end to end without gcc.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from langgraph.graph import END
from sqlalchemy import create_engine
from sqlmodel import Session

from app.agents import build as build_module
from app.agents import firmware as firmware_module
from app.agents import repair as repair_module
from app.agents.repair import (
    LineEdit,
    apply_edits,
    error_windows,
    repair_firmware,
    repairable_errors,
    should_repair,
)
from app.build import artifacts as artifacts_module
from app.build import workspace
from app.build.client import BuilderClient
from app.core.config import settings
from app.db.models import Project, TaskRun
from app.db.session import upgrade_database
from app.orchestrator.contracts import (
    BUILD_FAILED,
    BUILD_OK,
    BUILD_UNAVAILABLE,
    BuildResult,
    CubeMXPlan,
    Diagnostic,
    FirmwareBundle,
    SourceFile,
    dump,
)
from app.orchestrator.graph import REPAIR_AGENTS, _route_after_build, build_graph

PROJECT_ID = "p1"
PLAN = CubeMXPlan(mcu="STM32F407VGT6")
APP_PATH = "Core/Src/app.c"
TEMPLATE_PATH = "Core/Src/system_stm32f4xx.c"


def _source(line_count: int = 40) -> str:
    lines = [f"int line_{n}_marker = {n};" for n in range(1, line_count + 1)]
    lines[9] = "/* USER CODE BEGIN 0 */"
    lines[10] = "/* USER CODE END 0 */"
    return "\n".join(lines)


def _bundle(contents: str | None = None) -> FirmwareBundle:
    return FirmwareBundle(
        files=[
            SourceFile(path=APP_PATH, contents=contents or _source()),
            SourceFile(path=TEMPLATE_PATH, contents="void SystemInit(void) {}", generated=False),
        ]
    )


def _error(path: str = APP_PATH, line: int = 30, message: str = "'foo' undeclared") -> Diagnostic:
    return Diagnostic(file=path, line=line, column=5, severity="error", message=message)


def _failed(*errors: Diagnostic, attempt: int = 1) -> BuildResult:
    return BuildResult(
        status=BUILD_FAILED,
        exit_code=2,
        attempt=attempt,
        diagnostics=list(errors) or [_error()],
    )


class FakeLLM:
    def __init__(self, reply: dict | str):
        self.reply = reply if isinstance(reply, str) else json.dumps(reply)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        return self.reply


@pytest.fixture
def ws(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_root", str(tmp_path / "ws"))
    workspace.ensure_workspace(PROJECT_ID)
    workspace.write_file(PROJECT_ID, "Makefile", "all:\n\techo build\n")
    workspace.write_file(PROJECT_ID, APP_PATH, _source())
    url = f"sqlite:///{tmp_path / 'repair.db'}"
    engine = create_engine(url)
    upgrade_database(engine, url)
    monkeypatch.setattr(artifacts_module, "_get_engine", lambda: engine)
    with Session(engine) as session:
        session.add(Project(id=PROJECT_ID, name="demo", user_request="r"))
        session.commit()
        session.add(TaskRun(project_id=PROJECT_ID, agent_name="build"))
        session.commit()
    return tmp_path


# --------------------------------------------------------------------------
# Edits
# --------------------------------------------------------------------------


def test_edits_inside_a_shown_window_are_applied_bottom_up():
    lines = [f"l{n}" for n in range(1, 11)]
    windows = [(1, 10)]
    edits = [
        LineEdit(start_line=2, end_line=2, replacement="a\nb"),
        LineEdit(start_line=8, end_line=9, replacement=""),
    ]

    result, applied, rejected = apply_edits(lines, edits, windows)

    assert applied == 2
    assert rejected == []
    assert result == ["l1", "a", "b", "l3", "l4", "l5", "l6", "l7", "l10"]


def test_edits_outside_the_excerpts_or_overlapping_are_rejected():
    lines = [f"l{n}" for n in range(1, 41)]
    windows = [(1, 20), (24, 36)]
    edits = [
        LineEdit(start_line=30, end_line=30, replacement="fixed"),
        LineEdit(start_line=30, end_line=31, replacement="again"),
        LineEdit(start_line=38, end_line=38, replacement="far away"),
        LineEdit(start_line=19, end_line=25, replacement="spans a gap"),
        LineEdit(start_line=5, end_line=4, replacement="backwards"),
    ]

    result, applied, rejected = apply_edits(lines, edits, windows)

    assert applied == 1
    assert len(rejected) == 4
    assert result[29] == "fixed"
    assert result[37] == "l38"


def test_windows_cover_the_head_and_each_error():
    windows = error_windows(100, [_error(line=50), _error(line=53), _error(line=0)])

    assert windows == [(1, 60)]
    assert error_windows(100, [_error(line=80)]) == [(1, 20), (74, 86)]
    assert error_windows(0, [_error()]) == []


# --------------------------------------------------------------------------
# When to repair
# --------------------------------------------------------------------------


def test_only_errors_in_model_written_files_are_repairable():
    result = _failed(_error(), _error(path=TEMPLATE_PATH), _error(path="Drivers/x.c"))

    assert [d.file for d in repairable_errors(result, _bundle())] == [APP_PATH]


def test_repairable_errors_are_capped():
    result = _failed(*(_error(line=n) for n in range(1, 20)))

    assert len(repairable_errors(result, _bundle())) == repair_module.MAX_ERRORS


def test_should_repair_is_bounded_by_the_setting(monkeypatch):
    monkeypatch.setattr(settings, "firmware_build_retries", 2)
    bundle = _bundle()

    assert should_repair(_failed(), bundle, attempt=1)
    assert should_repair(_failed(), bundle, attempt=2)
    assert not should_repair(_failed(), bundle, attempt=3)

    monkeypatch.setattr(settings, "firmware_build_retries", 0)
    assert not should_repair(_failed(), bundle, attempt=1)


def test_nothing_to_repair_when_the_build_did_not_fail_on_our_files():
    bundle = _bundle()

    assert not should_repair(BuildResult(status=BUILD_OK), bundle, attempt=1)
    assert not should_repair(BuildResult(status=BUILD_UNAVAILABLE), bundle, attempt=1)
    assert not should_repair(_failed(_error(path=TEMPLATE_PATH)), bundle, attempt=1)


# --------------------------------------------------------------------------
# The repair step
# --------------------------------------------------------------------------


def test_repair_patches_the_workspace_and_shows_only_excerpts(ws):
    llm = FakeLLM(
        {
            "path": APP_PATH,
            "edits": [{"start_line": 30, "end_line": 30, "replacement": "int foo = 30;"}],
            "notes": ["foo was never declared"],
        }
    )

    bundle, warnings, report = asyncio.run(
        repair_firmware(_bundle(), _failed(), project_id=PROJECT_ID, context="SPI1", llm=llm)
    )

    on_disk = workspace.read_file(PROJECT_ID, APP_PATH).split("\n")
    assert on_disk[29] == "int foo = 30;"
    assert bundle.file(APP_PATH).contents.split("\n")[29] == "int foo = 30;"
    assert report["files"] == {APP_PATH: 1}
    assert report["notes"] == [f"{APP_PATH}: foo was never declared"]
    assert warnings == []

    prompt = llm.calls[0][1]["content"]
    assert "line_1_marker" in prompt  # include block
    assert "line_30_marker" in prompt  # the error line
    assert "line_40_marker" not in prompt  # never the whole file
    assert "'foo' undeclared" in prompt


def test_a_patch_that_drops_a_user_code_marker_is_rejected(ws):
    llm = FakeLLM(
        {
            "path": APP_PATH,
            "edits": [{"start_line": 10, "end_line": 11, "replacement": "int x;"}],
        }
    )

    bundle, warnings, report = asyncio.run(
        repair_firmware(_bundle(), _failed(), project_id=PROJECT_ID, llm=llm)
    )

    assert workspace.read_file(PROJECT_ID, APP_PATH) == _source()
    assert bundle.file(APP_PATH).contents == _source()
    assert any("USER CODE markers" in message for message in report["rejected"])
    assert any("no edit could be applied" in warning for warning in warnings)


def test_an_unusable_reply_leaves_the_files_alone(ws):
    bundle, warnings, _ = asyncio.run(
        repair_firmware(_bundle(), _failed(), project_id=PROJECT_ID, llm=FakeLLM("no JSON"))
    )

    assert workspace.read_file(PROJECT_ID, APP_PATH) == _source()
    assert any("no usable patch" in warning for warning in warnings)


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def _failing_builder(calls: list) -> BuilderClient:
    log = f"{settings.workspace_root}/{PROJECT_ID}/{APP_PATH}:30:5: error: 'foo' undeclared\n"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        payload = {
            "status": "failed",
            "exit_code": 2,
            "duration_ms": 10,
            "toolchain": "arm-none-eabi-gcc 12.2.1",
            "command": "make -j4",
            "log": log,
            "artifacts": {},
            "size_output": "",
        }
        return httpx.Response(200, json=payload)

    return BuilderClient(base_url="http://builder:9000", transport=httpx.MockTransport(handler))


def test_a_build_that_never_heals_stops_after_the_retry_budget(ws, monkeypatch):
    """Emulates the graph's firmware <-> build cycle with the real nodes."""
    monkeypatch.setattr(settings, "firmware_build_retries", 2)
    calls: list[httpx.Request] = []
    monkeypatch.setattr(build_module, "get_builder_client", lambda: _failing_builder(calls))
    llm = FakeLLM({"path": APP_PATH, "edits": [], "notes": ["cannot fix"]})
    monkeypatch.setattr(repair_module, "is_agent_enabled", lambda _name: True)
    monkeypatch.setattr(repair_module, "get_agent_llm", lambda _name: llm)

    state = {
        "project_id": PROJECT_ID,
        "cubemx": dump(PLAN),
        "firmware": dump(_bundle()),
        "attempt": 1,
    }

    async def run() -> list[int]:
        attempts = []
        while True:
            state.update(await build_module.build_node(state))
            attempts.append(state["build"]["attempt"])
            if _route_after_build(state) == END:
                return attempts
            state.update(await firmware_module.firmware_node(state))

    attempts = asyncio.run(run())

    assert attempts == [1, 2, 3]
    assert len(calls) == settings.firmware_build_retries + 1
    assert len(llm.calls) == settings.firmware_build_retries
    assert state["repair_next"] is False
    assert state["build_artifacts"]["repair_next"] is False
    assert state["firmware_artifacts"]["repair"]["from_attempt"] == 2


def test_route_after_build_reads_only_the_flag():
    assert _route_after_build({"repair_next": True}) == REPAIR_AGENTS[0]
    assert _route_after_build({"repair_next": False}) == END
    assert _route_after_build({}) == END


def test_graph_with_the_repair_edge_compiles():
    assert build_graph() is not None
