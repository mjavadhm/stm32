"""Generate a project for a real board and compile it. The P3b acceptance gate.

    make board                      # the default board
    make board BOARD=nucleo-f411re

`make scaffold` proves the generator works from a plan someone typed. This
proves it works from a plan nobody typed: the board profile supplies the
crystal and the pins, the solver supplies the PLL, the part's imported table
supplies the alternate-function numbers, and none of the three is a number
written into this file. What is left over is exactly the part a model has to
fill in, which is what P3c is for.
"""

import asyncio
import os
import sys

from app.build.client import BuilderClient
from app.codegen import boards, devicedata
from app.codegen.devices import device_for
from app.codegen.errors import CodegenError
from app.codegen.scaffold import scaffold_project
from app.codegen.validate import validate_plan
from app.orchestrator.contracts import BUILD_OK

DEFAULT_BOARD = "blackpill-f411"


def requested_board() -> str:
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    return os.environ.get("BOARD", "").strip() or DEFAULT_BOARD


def describe(board: boards.Board, plan) -> None:
    clock = plan.clock
    print(f"board     : {board.name} ({board.mcu})")
    print(f"facts     : {board.source}")
    print(
        f"clock     : {clock.source} {clock.hse_hz} Hz -> {clock.hclk_hz} Hz "
        f"(PLL M{clock.pll_m} N{clock.pll_n} P{clock.pll_p} Q{clock.pll_q})"
    )
    print(f"buses     : APB1 {clock.apb1_hz} Hz, APB2 {clock.apb2_hz} Hz")
    pins = ", ".join(f"{pin.pin}={pin.signal}" for pin in plan.pins)
    print(f"pins      : {pins or 'none'}")
    for warning in plan.warnings:
        print(f"  warning : {warning}")
    for note in plan.assumptions:
        print(f"  note    : {note}")


async def main() -> int:
    name = requested_board()
    try:
        board = boards.board_for(name)
        plan, _ = boards.plan_for(board)
    except CodegenError as error:
        print(f"FAIL  {error}")
        return 1

    describe(board, plan)
    device = device_for(board.part)

    try:
        data = devicedata.load(device.part)
    except CodegenError as error:
        print(f"FAIL  {error}")
        return 1

    report = validate_plan(plan, data=data)
    print(f"validated : {report.resolved}/{report.pins} alternate-function pins, {report.part}")
    print(f"table     : {report.source}")
    for warning in report.warnings:
        print(f"  warning : {warning}")
    for message in report.errors:
        print(f"  error   : {message}")
    if not report.ok:
        # The board profile claims a pin the part cannot use that way. That is
        # a bug in this repository, not in anyone's project.
        print(f"FAIL  the {board.name} profile disagrees with the part's pin table")
        return 1

    project = f"board_{board.name.replace('-', '_')}"
    scaffold = scaffold_project(project, plan, summary=f"{board.name} console scaffold.")
    print(f"device    : {scaffold.device}")
    print(f"sdk       : {scaffold.sdk_version or 'unknown'}")
    print(f"generated : {len(scaffold.files)} files, {len(scaffold.sources)} to compile")
    print(f"configured: {', '.join(scaffold.configured) or 'none'}")
    for warning in scaffold.warnings:
        print(f"  warning : {warning}")

    client = BuilderClient()
    try:
        health = await client.health()
        if health is None:
            print(f"FAIL  build sandbox not reachable at {client.base_url}")
            print("      try: make builder-image && docker compose up -d builder")
            return 1
        result = await client.build(
            project,
            clean=True,
            flash_total=device.flash_bytes,
            ram_total=device.ram_bytes,
        )
    finally:
        await client.aclose()

    print(f"status    : {result.status} (exit {result.exit_code}) in {result.duration_ms} ms")
    print(f"flash     : {result.size.flash_bytes} B ({result.size.flash_pct}%)")
    print(f"ram       : {result.size.ram_bytes} B ({result.size.ram_pct}%)")

    findings = [d for d in result.diagnostics if d.severity != "note"]
    for diagnostic in findings[:15]:
        print(f"  {diagnostic.as_prompt()}")

    if result.status != BUILD_OK or "elf" not in result.artifacts:
        print("FAIL  the generated project did not compile")
        print(result.log_tail)
        return 1
    if findings:
        print(f"WARN  the generated project compiles with {len(findings)} warning(s)")
        return 1
    print(f"OK    a project generated for {board.name} compiles clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
