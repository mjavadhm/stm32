"""Build node (M4 P5): compile the generated project in the sandbox.

Runs after the firmware agent. It never fails the pipeline: a project that
does not compile is still a delivered project, with its diagnostics attached
(docs/m4-plan.md, P5). The repair loop that feeds those diagnostics back to
the firmware agent is added on top of this node, not inside it.
"""

import logging
from typing import Any

from app.build import workspace
from app.build.artifacts import BUILD_LOG_PATH, record_artifacts
from app.build.client import BuilderClient, get_builder_client
from app.codegen.devices import device_for
from app.codegen.errors import CodegenError
from app.orchestrator.contracts import (
    BUILD_UNAVAILABLE,
    BuildResult,
    CubeMXPlan,
    dump,
    parse_stored,
)

logger = logging.getLogger(__name__)
AGENT_NAME = "build"
# How many errors the progress summary repeats; the full list is in `build`.
SUMMARY_ERRORS = 5


def memory_totals(mcu: str) -> tuple[int, int]:
    """(flash, ram) capacity in bytes, or zeros for a part we have no row for."""
    try:
        device = device_for(mcu)
    except CodegenError:
        return 0, 0
    return device.flash_bytes, device.ram_bytes


def _has_workspace(project_id: str) -> bool:
    try:
        return bool(project_id) and workspace.exists(project_id)
    except workspace.WorkspaceError:
        return False


async def run_build(
    project_id: str,
    plan: CubeMXPlan,
    *,
    attempt: int = 1,
    client: BuilderClient | None = None,
) -> BuildResult:
    """Compile one project workspace and index what it produced. Never raises."""
    if not _has_workspace(project_id):
        message = (
            "no project workspace to build: the CubeMX/firmware steps did not "
            "write any files for this project"
        )
        logger.warning("%s (%s)", message, project_id)
        return BuildResult(
            status=BUILD_UNAVAILABLE, exit_code=-1, attempt=attempt, log_tail=message
        )

    flash_total, ram_total = memory_totals(plan.mcu)
    builder = client or get_builder_client()
    result = await builder.build(
        project_id,
        clean=True,
        attempt=attempt,
        flash_total=flash_total,
        ram_total=ram_total,
    )

    if result.log_tail:
        try:
            workspace.write_file(project_id, BUILD_LOG_PATH, result.log_tail)
        except Exception:  # noqa: BLE001 - the log is a convenience copy
            logger.debug("could not write %s for %s", BUILD_LOG_PATH, project_id, exc_info=True)
    await record_artifacts(project_id, attempt, result)
    return result


def summarise_result(result: BuildResult) -> dict[str, Any]:
    """Small progress-view payload; the full contract is stored under `build`."""
    return {
        "status": result.status,
        "attempt": result.attempt,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "toolchain": result.toolchain,
        "flash_bytes": result.size.flash_bytes,
        "ram_bytes": result.size.ram_bytes,
        "flash_pct": result.size.flash_pct,
        "ram_pct": result.size.ram_pct,
        "errors": len(result.errors),
        "warnings": len(result.warnings),
        "first_errors": [d.as_prompt() for d in result.first_errors(SUMMARY_ERRORS)],
        "artifacts": dict(result.artifacts),
    }


async def build_node(state: dict[str, Any]) -> dict[str, Any]:
    """LangGraph node for the build step."""
    project_id = state.get("project_id", "")
    attempt = int(state.get("attempt") or 1)
    plan = parse_stored(CubeMXPlan, state.get("cubemx"))

    result = await run_build(project_id, plan, attempt=attempt)
    logger.info(
        "build %s attempt %d: %s (%d errors, %d warnings)",
        project_id,
        attempt,
        result.status,
        len(result.errors),
        len(result.warnings),
    )
    return {"build": dump(result), "build_artifacts": summarise_result(result)}
