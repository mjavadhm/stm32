"""The build sandbox, tested without a cross compiler (M4, P0/P1).

The toolchain sits behind an HTTP boundary, so everything the backend does
with a build -- parse the log, resolve paths, degrade when the sandbox is
gone -- is testable with a mock transport and a temp directory. The one thing
these tests cannot prove is that gcc itself works; that is what `make golden`
is for, and it runs in CI.
"""

import asyncio
import json
import stat
import tempfile
from pathlib import Path

import httpx
import pytest

from app.build import workspace
from app.build.client import BuilderClient
from app.build.diagnostics import parse_log, parse_size, summarise
from app.core.config import settings
from app.orchestrator.contracts import (
    BUILD_FAILED,
    BUILD_OK,
    BUILD_TIMEOUT,
    BUILD_UNAVAILABLE,
    BuildResult,
    BuildSize,
    CubeMXPlan,
    Diagnostic,
    FirmwareBundle,
    PinAssignment,
    SourceFile,
    dump,
    parse_stored,
)

FIXTURES = Path(__file__).parent / "fixtures"
GCC_LOG = (FIXTURES / "gcc_errors.txt").read_text(encoding="utf-8")
WORKSPACE_ROOT = "/workspaces/7f3a1c"

SIZE_OUTPUT = (
    "   text\t   data\t    bss\t    dec\t    hex\tfilename\n"
    "  12000\t    120\t   2048\t  14168\t   3758\tbuild/app.elf\n"
)

OK_PAYLOAD = {
    "status": "ok",
    "exit_code": 0,
    "duration_ms": 4200,
    "toolchain": "arm-none-eabi-gcc (15:12.2.rel1-1) 12.2.1 20221205",
    "command": "make -j4",
    "log": "arm-none-eabi-size build/app.elf\n",
    "artifacts": {"elf": "build/app.elf", "bin": "build/app.bin"},
    "size_output": SIZE_OUTPUT,
}


def _build(handler, **kwargs) -> BuildResult:
    """Run one build against a mocked sandbox."""

    async def run() -> BuildResult:
        client = BuilderClient(
            base_url="http://builder:9000",
            transport=httpx.MockTransport(handler),
        )
        try:
            return await client.build(kwargs.pop("project_id", "7f3a1c"), **kwargs)
        finally:
            await client.aclose()

    return asyncio.run(run())


# --------------------------------------------------------------------------
# Reading compiler output
# --------------------------------------------------------------------------


def test_compiler_errors_become_structured_diagnostics():
    diagnostics = parse_log(GCC_LOG, root=WORKSPACE_ROOT)

    errors = [d for d in diagnostics if d.severity == "error"]
    first = errors[0]
    assert first.file == "Core/Src/main.c"
    assert first.line == 42
    assert first.column == 5
    assert "'hspi1' undeclared" in first.message
    # The "note:" follow-up is kept, but never as an error.
    assert any(d.severity == "note" for d in diagnostics)


def test_warning_codes_survive_parsing():
    warnings = [d for d in parse_log(GCC_LOG) if d.severity == "warning"]

    codes = {warning.code for warning in warnings}
    assert "-Wunused-variable" in codes
    assert "-Wreturn-type" in codes
    # The code is stripped out of the message, not left dangling in it.
    assert not any("[-W" in warning.message for warning in warnings)


def test_the_workspace_prefix_never_reaches_the_model():
    absolute = parse_log(GCC_LOG)
    relative = parse_log(GCC_LOG, root=WORKSPACE_ROOT)

    assert absolute[0].file.startswith("/workspaces/")
    assert not any(d.file.startswith("/workspaces/") for d in relative)
    assert relative[0].as_prompt().startswith("Core/Src/main.c:42:")


def test_linker_errors_point_at_the_missing_symbol():
    linker = [d for d in parse_log(GCC_LOG, root=WORKSPACE_ROOT) if d.tool == "ld"]

    undefined = [d for d in linker if "undefined reference" in d.message]
    assert undefined[0].file == "Core/Src/mpu6050.c"
    assert undefined[0].line == 31
    assert "HAL_I2C_Mem_Write" in undefined[0].message
    # "in function `MPU6050_Init':" is context, not a second finding.
    assert not any(d.message.endswith("':") for d in linker)
    assert any("region RAM overflowed" in d.message for d in linker)


def test_linker_notes_from_a_green_build_are_not_errors():
    ld = "/usr/lib/gcc/arm-none-eabi/14.2.1/../../../arm-none-eabi/bin/ld: "
    lib = "/usr/lib/gcc/arm-none-eabi/14.2.1/../../../arm-none-eabi/lib/libc_nano.a"
    log = (
        f"{ld}{lib}(libc_a-closer.o): in function `_close_r':\n"
        f"{ld}closer.c:(.text._close_r+0xc): warning: _close is not implemented\n"
        f"{ld}{lib}(libc_a-closer.o): note: the message above does not take "
        "linker garbage collection into account\n"
    )

    diagnostics = parse_log(log)

    # This exact output comes out of a *successful* golden build. Calling any
    # of it an error would hand the repair loop a build with nothing to fix.
    assert [d.severity for d in diagnostics] == ["warning", "note"]
    assert diagnostics[0].file == "closer.c"
    assert diagnostics[1].file == ""
    assert summarise(diagnostics).startswith("0 error(s), 1 warning(s)")


def test_make_noise_is_dropped_when_a_real_error_exists():
    diagnostics = parse_log(GCC_LOG, root=WORKSPACE_ROOT)

    assert not any(d.tool == "make" for d in diagnostics)


def test_make_failure_is_kept_when_nothing_else_explains_it():
    log = "make: *** No rule to make target 'Core/Src/mpu6050.c'.  Stop.\n"

    diagnostics = parse_log(log)

    assert len(diagnostics) == 1
    assert diagnostics[0].tool == "make"
    assert "No rule to make target" in diagnostics[0].message


def test_a_make_failure_points_at_the_makefile_line():
    log = "make: *** [Makefile:27: build] Error 1\n"

    diagnostic = parse_log(log)[0]

    # Otherwise this prints as "<unknown>: error: ..." and tells nobody where
    # to look.
    assert diagnostic.file == "Makefile"
    assert diagnostic.line == 27


def test_a_recipe_that_dies_before_gcc_is_reported_as_the_cause():
    log = (
        "mkdir -p build\n"
        "mkdir: cannot create directory 'build': Permission denied\n"
        "make: *** [Makefile:27: build] Error 1\n"
    )

    diagnostics = parse_log(log)

    assert [d.tool for d in diagnostics] == ["shell"]
    assert "Permission denied" in diagnostics[0].message
    # A real cause silences the make summary, same as a compiler error would.
    assert not any(d.tool == "make" for d in diagnostics)


def test_duplicate_messages_are_reported_once():
    log = GCC_LOG + GCC_LOG

    assert parse_log(log, root=WORKSPACE_ROOT) == parse_log(GCC_LOG, root=WORKSPACE_ROOT)


def test_size_output_becomes_flash_and_ram():
    size = parse_size(SIZE_OUTPUT, flash_total=1024 * 1024, ram_total=128 * 1024)

    assert size.text == 12000
    assert size.flash_bytes == 12120  # text + data: initialisers live in flash
    assert size.ram_bytes == 2168  # data + bss
    assert size.flash_pct == 1.2
    assert size.ram_pct == 1.7


def test_an_unknown_device_reports_bytes_without_a_percentage():
    size = parse_size(SIZE_OUTPUT)

    assert size.flash_bytes == 12120
    assert size.flash_pct == 0.0


def test_a_summary_leads_with_the_first_error():
    summary = summarise(parse_log(GCC_LOG, root=WORKSPACE_ROOT))

    assert summary.startswith("4 error(s), 2 warning(s)")
    assert "Core/Src/main.c:42" in summary


# --------------------------------------------------------------------------
# Talking to the sandbox
# --------------------------------------------------------------------------


def test_a_successful_build_reports_artifacts_and_size():
    seen: dict[str, object] = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_PAYLOAD)

    result = _build(handler, clean=True, flash_total=1024 * 1024, ram_total=128 * 1024)

    assert seen["url"].endswith("/build")
    assert seen["body"]["project_id"] == "7f3a1c"
    assert seen["body"]["clean"] is True
    assert result.status == BUILD_OK
    assert result.ok
    assert result.artifacts["elf"] == "build/app.elf"
    assert result.size.flash_pct == 1.2
    assert result.toolchain.startswith("arm-none-eabi-gcc")
    assert result.diagnostics == []


def test_a_failed_build_returns_diagnostics_instead_of_raising():
    def handler(request):
        return httpx.Response(
            200,
            json={**OK_PAYLOAD, "status": "failed", "exit_code": 2, "log": GCC_LOG},
        )

    result = _build(handler, attempt=2)

    assert result.status == BUILD_FAILED
    assert not result.ok
    assert result.attempt == 2
    assert len(result.errors) == 4  # 2 from gcc, 2 from ld
    assert len(result.warnings) == 2
    # Paths are relative because the client knows which workspace it asked for.
    assert result.errors[0].file == "Core/Src/main.c"
    # The repair loop sees a handful of errors, not the whole cascade.
    assert len(result.first_errors(limit=2)) == 2
    assert "error:" in result.log_tail


def test_an_unreachable_sandbox_degrades_to_a_result():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    result = _build(handler)

    assert result.status == BUILD_UNAVAILABLE
    assert result.exit_code == -1
    assert "unavailable" in result.log_tail


def test_a_build_that_never_answers_is_reported_as_a_timeout():
    def handler(request):
        raise httpx.TimeoutException("read timeout")

    result = _build(handler)

    assert result.status == BUILD_TIMEOUT
    assert "timed out" in result.log_tail


def test_a_sandbox_error_response_does_not_break_the_run():
    def handler(request):
        return httpx.Response(404, json={"detail": "no workspace 'ghost'"})

    result = _build(handler, project_id="ghost")

    assert result.status == BUILD_UNAVAILABLE
    assert not result.ok


# --------------------------------------------------------------------------
# Workspaces
# --------------------------------------------------------------------------


def test_a_project_id_cannot_be_a_path():
    for bad in ["../etc", "/etc", "", "a b", "x" * 65, ".hidden"]:
        with pytest.raises(workspace.WorkspaceError):
            workspace.workspace_path(bad)


def test_a_generated_path_cannot_escape_the_workspace(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(settings, "workspace_root", tmp)
        workspace.ensure_workspace("demo")

        for bad in ["../../app/main.py", "/etc/passwd", "Core/../../escape.c", ""]:
            with pytest.raises(workspace.WorkspaceError):
                workspace.write_file("demo", bad, "int main(void) { return 0; }")


def test_files_are_written_read_and_listed(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(settings, "workspace_root", tmp)

        written = workspace.write_files(
            "demo",
            [
                SourceFile(path="Core/Src/main.c", contents="int main(void) { return 0; }"),
                SourceFile(path="Makefile", contents="all:\n\techo build\n"),
            ],
            clean=True,
        )
        workspace.write_file("demo", "build/app.elf", "pretend-binary")

        assert written == ["Core/Src/main.c", "Makefile"]
        # Build output is not project source and is never listed as such.
        assert workspace.list_files("demo") == ["Core/Src/main.c", "Makefile"]
        assert "build/app.elf" in workspace.list_files("demo", include_build=True)
        assert workspace.read_file("demo", "Core/Src/main.c").startswith("int main")


def test_a_clean_workspace_forgets_the_previous_run(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(settings, "workspace_root", tmp)
        workspace.write_file("demo", "Core/Src/old.c", "/* stale */")

        workspace.write_files(
            "demo",
            [SourceFile(path="Core/Src/new.c", contents="/* fresh */")],
            clean=True,
        )

        assert workspace.list_files("demo") == ["Core/Src/new.c"]


def test_a_copied_project_stays_writable_for_the_build_container(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(settings, "workspace_root", str(Path(tmp) / "workspaces"))
        fixture = Path(tmp) / "fixture"
        (fixture / "Core" / "Src").mkdir(parents=True)
        (fixture / "Makefile").write_text("all:\n\techo build\n", encoding="utf-8")
        (fixture / "Core" / "Src" / "main.c").write_text("int main(void){}", encoding="utf-8")
        # What a git checkout looks like: not writable by anyone else.
        (fixture / "Core" / "Src").chmod(0o755)
        (fixture / "Core").chmod(0o755)
        fixture.chmod(0o755)

        workspace.copy_tree(fixture, "golden")

        # The builder runs as another uid and has to create build/ in here.
        # copytree's copystat used to hand it a read-only directory instead.
        for relative in [".", "Core", "Core/Src"]:
            path = workspace.workspace_path("golden") / relative
            assert stat.S_IMODE(path.stat().st_mode) == 0o777, relative
        assert workspace.list_files("golden") == ["Core/Src/main.c", "Makefile"]


def test_a_written_bundle_stays_writable_for_the_build_container(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(settings, "workspace_root", tmp)

        workspace.write_files(
            "demo",
            [SourceFile(path="Core/Src/main.c", contents="int main(void){}")],
            clean=True,
        )

        for relative in [".", "Core", "Core/Src"]:
            path = workspace.workspace_path("demo") / relative
            assert stat.S_IMODE(path.stat().st_mode) == 0o777, relative


def test_the_golden_project_ships_with_everything_it_needs():
    golden = FIXTURES / "golden-f407-blinky"

    names = {path.name for path in golden.iterdir()}
    assert {"Makefile", "main.c", "startup_stm32f407xx.c", "STM32F407VGTx_FLASH.ld"} <= names
    linker = (golden / "STM32F407VGTx_FLASH.ld").read_text(encoding="utf-8")
    # The startup code copies .data using these symbols; if the script stops
    # defining them the golden build fails for a reason nobody expects.
    for symbol in ["_estack", "_sidata", "_sdata", "_edata", "_sbss", "_ebss"]:
        assert symbol in linker


# --------------------------------------------------------------------------
# M4 contracts
# --------------------------------------------------------------------------


def test_a_build_result_survives_storage():
    result = BuildResult(
        status=BUILD_FAILED,
        exit_code=2,
        attempt=2,
        diagnostics=[
            Diagnostic(file="Core/Src/main.c", line=42, message="'hspi1' undeclared"),
            Diagnostic(file="Core/Src/main.c", line=88, severity="warning", message="unused"),
        ],
        size=BuildSize(text=12000, data=120, bss=2048, flash_total=1024 * 1024),
    )

    restored = parse_stored(BuildResult, json.loads(json.dumps(dump(result))))

    assert restored.attempt == 2
    assert [d.line for d in restored.errors] == [42]
    assert restored.size.flash_bytes == 12120


def test_peripheral_handles_match_the_generated_init_code():
    plan = CubeMXPlan(mcu="STM32F407VG")

    assert plan.handle("SPI1") == "hspi1"
    assert plan.handle("USART2") == "huart2"
    assert plan.handle("UART4") == "huart4"
    assert plan.handle("i2c1") == "hi2c1"


def test_a_plan_is_not_validated_until_something_checks_it():
    plan = CubeMXPlan(
        mcu="STM32F407VG",
        pins=[PinAssignment(pin="PA5", signal="SPI1_SCK", peripheral="SPI1")],
    )

    assert plan.validated is False
    # The alternate-function number is looked up, never guessed by the model.
    assert plan.pins[0].alternate is None


def test_a_bundle_knows_which_step_produced_a_file():
    bundle = FirmwareBundle(
        files=[
            SourceFile(path="Core/Src/main.c", step_order=1),
            SourceFile(path="Core/Src/mpu6050.c", step_order=2, generated=True),
            SourceFile(path="STM32F407VGTx_FLASH.ld", generated=False),
        ]
    )

    assert bundle.paths[1] == "Core/Src/mpu6050.c"
    assert bundle.file("Core/Src/mpu6050.c").step_order == 2
    assert bundle.file("nowhere.c") is None
    # Templates are ours to fix; only generated files go back to the model.
    assert [f.path for f in bundle.files if f.generated] == [
        "Core/Src/main.c",
        "Core/Src/mpu6050.c",
    ]
