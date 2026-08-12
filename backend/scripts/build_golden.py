"""Smoke test for the build sandbox: compile a project known to be good.

Run this before blaming a generated project for a compile error. If the
golden project does not build, the toolchain, the volume or the network
wiring is broken -- not the model.

    make golden
"""

import asyncio
from pathlib import Path

from app.build.client import BuilderClient
from app.build.workspace import copy_tree
from app.orchestrator.contracts import BUILD_OK

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden-f407-blinky"
PROJECT_ID = "golden"
# STM32F407VG: 1 MB flash, 128 KB SRAM (CCM excluded on purpose).
FLASH_TOTAL = 1024 * 1024
RAM_TOTAL = 128 * 1024


async def main() -> int:
    client = BuilderClient()
    try:
        health = await client.health()
        if health is None:
            print(f"FAIL  build sandbox not reachable at {client.base_url}")
            print("      try: make builder-image && docker compose up -d builder")
            return 1
        print(f"toolchain : {health.get('toolchain', '?')}")

        workspace = copy_tree(FIXTURE, PROJECT_ID)
        print(f"workspace : {workspace}")
        result = await client.build(
            PROJECT_ID,
            clean=True,
            flash_total=FLASH_TOTAL,
            ram_total=RAM_TOTAL,
        )
    finally:
        await client.aclose()

    print(f"command   : {result.command}")
    print(f"status    : {result.status} (exit {result.exit_code}) in {result.duration_ms} ms")
    artifacts = ", ".join(f"{kind}={path}" for kind, path in sorted(result.artifacts.items()))
    print(f"artifacts : {artifacts or 'none'}")
    print(f"flash     : {result.size.flash_bytes} B ({result.size.flash_pct}%)")
    print(f"ram       : {result.size.ram_bytes} B ({result.size.ram_pct}%)")
    # Notes are context for an error. On a green build they are just newlib
    # explaining that it cannot see linker garbage collection.
    findings = [d for d in result.diagnostics if d.severity != "note"]
    for diagnostic in findings[:10]:
        print(f"  {diagnostic.as_prompt()}")

    if result.status != BUILD_OK or "elf" not in result.artifacts:
        print("FAIL  the golden project did not build")
        print(result.log_tail)
        return 1
    print("OK    the sandbox compiles a known-good project")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
