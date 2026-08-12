"""Isolated build service (M4).

This container is a toolchain with a doorbell. It runs `make` inside one
workspace directory and returns the raw outcome: exit code, merged log, the
artifacts it found and the output of `arm-none-eabi-size`.

It deliberately does **not** interpret that log. Turning compiler output into
structured diagnostics happens in the backend (`app/build/diagnostics.py`),
next to the repair loop that consumes it and where it can be unit tested
without a cross compiler. Business logic living in two images is how two
implementations of the same rule start to disagree.
"""

import asyncio
import os
import re
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspaces"))
CUBE_SDK_ROOT = Path(os.environ.get("CUBE_SDK_ROOT", "/opt/stm32cube/f4"))
DEFAULT_TIMEOUT = float(os.environ.get("BUILD_TIMEOUT_SECONDS", "120"))
MAX_LOG_CHARS = int(os.environ.get("BUILD_MAX_LOG_CHARS", "20000"))
CLEAN_TIMEOUT = 30.0

# A workspace name is a project id, not a path. Anything else is refused
# before it can walk out of the volume with "..".
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

ARTIFACT_SUFFIXES = ("elf", "bin", "hex", "map")

app = FastAPI(title="STM32 build sandbox", version="1.0")


class BuildRequest(BaseModel):
    project_id: str
    target: str = ""  # "" = the Makefile's default target
    timeout_seconds: float | None = None
    clean: bool = False
    jobs: int = 4


def workspace_for(project_id: str) -> Path:
    if not SAFE_NAME.match(project_id or ""):
        raise HTTPException(status_code=400, detail="invalid project_id")
    path = WORKSPACE_ROOT / project_id
    if not path.is_dir():
        raise HTTPException(status_code=404, detail=f"no workspace {project_id!r}")
    return path


def clip(text: str, limit: int = MAX_LOG_CHARS) -> str:
    """Keep both ends of a long log.

    With `make -j` the first error can be thousands of lines from the end, so
    keeping only the tail (the usual choice) throws away the cause and keeps
    the consequences.
    """
    if len(text) <= limit:
        return text
    head = int(limit * 0.4)
    tail = limit - head
    dropped = len(text) - limit
    return f"{text[:head]}\n\n[... {dropped} characters omitted ...]\n\n{text[-tail:]}"


async def run(cmd: list[str], cwd: Path, timeout: float) -> tuple[int, str, bool]:
    """Run a command with merged output. Returns (exit_code, log, timed_out)."""
    started = asyncio.get_running_loop().time()
    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": "/tmp",
            "LC_ALL": "C",  # stable, parseable compiler messages
        },
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        elapsed = asyncio.get_running_loop().time() - started
        return -1, f"build killed after {elapsed:.0f}s (limit {timeout:.0f}s)", True
    return process.returncode or 0, stdout.decode("utf-8", "replace"), False


async def capture(cmd: list[str]) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await process.communicate()
    except OSError as exc:
        return f"unavailable: {exc}"
    return stdout.decode("utf-8", "replace").strip()


async def toolchain_version() -> str:
    first_line = (await capture(["arm-none-eabi-gcc", "--version"])).splitlines()
    return first_line[0] if first_line else "unknown"


def find_artifacts(workspace: Path) -> dict[str, str]:
    """Newest artifact of each kind, as a workspace-relative path."""
    found: dict[str, str] = {}
    for suffix in ARTIFACT_SUFFIXES:
        matches = sorted(
            workspace.rglob(f"*.{suffix}"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if matches:
            found[suffix] = str(matches[0].relative_to(workspace))
    return found


def sdk_version() -> str:
    """Which driver sources this image downloaded, or why it has none."""
    try:
        return " | ".join((CUBE_SDK_ROOT / "VERSION").read_text("utf-8").split())
    except OSError:
        return "missing"


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "toolchain": await toolchain_version(),
        "make": (await capture(["make", "--version"])).splitlines()[0:1],
        "workspace_root": str(WORKSPACE_ROOT),
        "sdk_root": str(CUBE_SDK_ROOT),
        "sdk": sdk_version(),
    }


@app.post("/build")
async def build(request: BuildRequest) -> dict:
    workspace = workspace_for(request.project_id)
    timeout = request.timeout_seconds or DEFAULT_TIMEOUT
    started = time.perf_counter()

    logs: list[str] = []
    if request.clean:
        _code, log, _timed_out = await run(["make", "clean"], workspace, CLEAN_TIMEOUT)
        logs.append(log)

    command = ["make", f"-j{max(1, request.jobs)}"]
    if request.target:
        command.append(request.target)
    exit_code, log, timed_out = await run(command, workspace, timeout)
    logs.append(log)

    artifacts = find_artifacts(workspace)
    size_output = ""
    if "elf" in artifacts:
        size_output = await capture(
            ["arm-none-eabi-size", str(workspace / artifacts["elf"])]
        )

    if timed_out:
        status = "timeout"
    elif exit_code == 0:
        status = "ok"
    else:
        status = "failed"

    return {
        "status": status,
        "exit_code": exit_code,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "toolchain": await toolchain_version(),
        "command": " ".join(command),
        "log": clip("\n".join(part for part in logs if part)),
        "artifacts": artifacts,
        "size_output": size_output,
    }
