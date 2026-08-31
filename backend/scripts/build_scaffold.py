"""Generate a project from a plan and compile it. The P2 acceptance gate.

The plan below is written by hand on purpose: it is the output the CubeMX
agent will have to produce in P3, so if this compiles, the only thing missing
from "a model designs a board and the firmware builds" is the model.

    make scaffold

It asks for one of everything the generator handles -- GPIO output, an
alternate-function pin group, a peripheral with an interrupt, and one with
two interrupt vectors -- so a regression shows up here rather than in a
user's project.
"""

import asyncio

from app.build.client import BuilderClient
from app.codegen import devicedata
from app.codegen.devices import device_for
from app.codegen.errors import CodegenError
from app.codegen.scaffold import scaffold_project
from app.codegen.validate import validate_plan
from app.orchestrator.contracts import (
    BUILD_OK,
    ClockPlan,
    CubeMXPlan,
    DmaConfig,
    PeripheralConfig,
    PinAssignment,
)

PROJECT_ID = "scaffold"
FLASH_TOTAL = 1024 * 1024
RAM_TOTAL = 128 * 1024

PLAN = CubeMXPlan(
    mcu="STM32F407VGT6",
    board="STM32F4 Discovery",
    # 8 MHz crystal -> 168 MHz, the standard Discovery clock tree.
    clock=ClockPlan(
        source="hse",
        hse_hz=8_000_000,
        pll_m=8,
        pll_n=336,
        pll_p=2,
        pll_q=7,
        sysclk_hz=168_000_000,
        hclk_hz=168_000_000,
        apb1_hz=42_000_000,
        apb2_hz=84_000_000,
        citation="RM0090:6.3.2",
    ),
    pins=[
        PinAssignment(pin="PD12", signal="LED_GREEN", mode="output", speed="low"),
        PinAssignment(pin="PD13", signal="LED_ORANGE", mode="output", speed="low"),
        PinAssignment(pin="PA0", signal="USER_BUTTON", mode="input", pull="down"),
        PinAssignment(pin="PA2", signal="USART2_TX", peripheral="USART2", alternate=7),
        PinAssignment(pin="PA3", signal="USART2_RX", peripheral="USART2", alternate=7),
        PinAssignment(pin="PA5", signal="SPI1_SCK", peripheral="SPI1", alternate=5),
        PinAssignment(pin="PA6", signal="SPI1_MISO", peripheral="SPI1", alternate=5),
        PinAssignment(pin="PA7", signal="SPI1_MOSI", peripheral="SPI1", alternate=5),
        PinAssignment(pin="PB8", signal="I2C1_SCL", peripheral="I2C1", alternate=4),
        PinAssignment(pin="PB9", signal="I2C1_SDA", peripheral="I2C1", alternate=4),
    ],
    peripherals=[
        PeripheralConfig(
            peripheral="USART2",
            mode="asynchronous",
            parameters={"BaudRate": "115200"},
            nvic_priority=5,
        ),
        PeripheralConfig(
            peripheral="SPI1",
            mode="master_full_duplex",
            # Written the short way on purpose: the generator completes it to
            # SPI_BAUDRATEPRESCALER_16.
            parameters={"BaudRatePrescaler": "16", "CLKPolarity": "LOW"},
            dma=[
                DmaConfig(
                    request="SPI1_RX",
                    stream="DMA2_Stream0",
                    channel=3,
                    direction="peripheral_to_memory",
                    nvic_priority=5,
                ),
                DmaConfig(
                    request="SPI1_TX",
                    stream="DMA2_Stream3",
                    channel=3,
                    direction="memory_to_peripheral",
                    nvic_priority=5,
                ),
            ],
        ),
        PeripheralConfig(
            peripheral="I2C1",
            mode="i2c",
            parameters={"ClockSpeed": "400000", "DutyCycle": "16_9"},
            nvic_priority=6,
        ),
        PeripheralConfig(
            peripheral="TIM3",
            mode="time_base",
            parameters={"Prescaler": "8399", "Period": "9999"},
            nvic_priority=7,
        ),
    ],
    validated=True,
)


def checked_plan() -> CubeMXPlan | None:
    """The plan with its alternate functions looked up instead of typed.

    The AF numbers above were written by hand from the datasheet, which is
    exactly what P3 exists to stop doing. So this strips them, asks the
    part's table to fill them in, and fails if the two disagree: one of the
    strongest checks available on the import, because the hand-written
    numbers are known to produce a board that works.

    On a machine that has not imported the tables yet, it says so and carries
    on with the hand-written ones -- `make scaffold` tests the generator, and
    should not start failing because of a step that belongs to `make devices`.
    """
    expected = {pin.pin: pin.alternate for pin in PLAN.pins if pin.alternate is not None}
    part = device_for(PLAN.mcu).part
    try:
        data = devicedata.load(part)
    except CodegenError as error:
        print(f"validated : no, no pin table for {part}")
        print(f"            {error}")
        return PLAN

    plan = PLAN.model_copy(deep=True)
    for assignment in plan.pins:
        assignment.alternate = None
    plan.validated = False
    report = validate_plan(plan, data=data)
    print(f"validated : {report.resolved}/{report.pins} alternate-function pins, {report.part}")
    print(f"table     : {report.source}")
    for warning in report.warnings:
        print(f"  warning : {warning}")
    for message in report.errors:
        print(f"  error   : {message}")
    if not report.ok:
        print("FAIL  the plan does not match the part")
        return None

    found = {pin.pin: pin.alternate for pin in plan.pins if pin.alternate is not None}
    if found != expected:
        print("FAIL  the table disagrees with the datasheet rows in this file")
        print(f"      by hand: {expected}")
        print(f"      table  : {found}")
        return None
    return plan


async def main() -> int:
    plan = checked_plan()
    if plan is None:
        return 1
    scaffold = scaffold_project(PROJECT_ID, plan, summary="P2 scaffold acceptance test.")
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
            PROJECT_ID,
            clean=True,
            flash_total=FLASH_TOTAL,
            ram_total=RAM_TOTAL,
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
    # A warning here is a defect in the generator, not in a user's code:
    # every line of this project was written by us.
    if findings:
        print(f"WARN  the generated project compiles with {len(findings)} warning(s)")
        return 1
    print("OK    a project generated from a plan compiles clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
