"""The project checker, the repair guard and the chip-select pin, offline.

The first real MPU6050 run "succeeded": it compiled after the repair loop
deleted the failing calls, left `/* Sensor read omitted */` in main(), kept
a commented-out CS write and a `0x6B\u793e` macro. Every case below is a piece
of that project.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.agents import build as build_module
from app.agents.repair import guard_patch, repair_firmware, repairable_errors, should_repair
from app.build import workspace
from app.build.client import BuilderClient
from app.codegen import checks
from app.codegen.devicedata import DeviceData
from app.codegen.peripherals import gpio_init
from app.codegen.select import complete_plan
from app.core.config import settings
from app.orchestrator.contracts import (
    BUILD_INCOMPLETE,
    BUILD_OK,
    BuildResult,
    CubeMXPlan,
    Diagnostic,
    FirmwareBundle,
    PeripheralConfig,
    PinAssignment,
    SourceFile,
)

HEADER = """#ifndef MPU6050_H
#define MPU6050_H
#ifdef __cplusplus
extern "C" {
#endif
#include "main.h"
typedef struct {
    int16_t Accel_X;
} MPU6050_Data_t;
HAL_StatusTypeDef MPU6050_Init(void);
HAL_StatusTypeDef MPU6050_Read_All_DMA(MPU6050_Data_t *data);
#ifdef __cplusplus
}
#endif
#endif
"""

DRIVER = """#include "mpu6050.h"
#define MPU6050_REG_PWR_MGMT_1  0x6B\u793e
static void MPU6050_Select(void)
{
  /* HAL_GPIO_WritePin(GPIOA, GPIO_PIN_4, GPIO_PIN_RESET); */
}
HAL_StatusTypeDef MPU6050_Init(void)
{
  MPU6050_Select();
  return HAL_OK;
}
HAL_StatusTypeDef MPU6050_ReadSensorData(MPU6050_Data_t *out)
{
  out->Accel_X = 0;
  return HAL_OK;
}
"""

MAIN_HOLLOW = """#include "main.h"
/* USER CODE BEGIN Includes */
#include "mpu6050.h"
/* USER CODE END Includes */
void Error_Handler(void);
int main(void)
{
  HAL_Init();
  /* USER CODE BEGIN 2 */
  /* Initialization omitted or adapted */
  /* USER CODE END 2 */
  while (1)
  {
    /* USER CODE BEGIN WHILE */
    HAL_Delay(100);
    /* USER CODE END WHILE */
  }
}
void Error_Handler(void)
{
  /* a template comment that mentions HAL_Delay(1); is not the model's */
  while (1) {}
}
"""

MAIN_WIRED = MAIN_HOLLOW.replace(
    "  /* Initialization omitted or adapted */", "  MPU6050_Init();"
).replace("    HAL_Delay(100);", "    MPU6050_Read_All_DMA(&data);\n    HAL_Delay(100);")

GOOD_DRIVER = """#include "mpu6050.h"
HAL_StatusTypeDef MPU6050_Init(void)
{
  HAL_GPIO_WritePin(SPI1_CS_GPIO_Port, SPI1_CS_Pin, GPIO_PIN_RESET);
  return HAL_OK;
}
HAL_StatusTypeDef MPU6050_Read_All_DMA(MPU6050_Data_t *data)
{
  data->Accel_X = 0;
  return HAL_OK;
}
"""


def _project(main: str = MAIN_HOLLOW, driver: str = DRIVER) -> dict[str, str]:
    return {
        "Core/Src/main.c": main,
        "Core/Inc/mpu6050.h": HEADER,
        "Core/Src/mpu6050.c": driver,
        "Core/Src/system_stm32f4xx.c": "void SystemInit(void) { /* TODO */ }\n",
    }


def _codes(findings: list[Diagnostic]) -> list[tuple[str, int, str]]:
    return [(d.file, d.line, d.code) for d in findings]


# --------------------------------------------------------------------------
# The checker
# --------------------------------------------------------------------------


def test_the_hollow_demo_project_is_caught_on_every_count():
    findings = checks.check_sources(_project())
    codes = _codes(findings)

    assert ("Core/Src/main.c", 9, "check-unreachable") in codes  # USER CODE BEGIN 2
    assert ("Core/Src/main.c", 15, "check-unreachable") in codes  # USER CODE BEGIN WHILE
    assert ("Core/Inc/mpu6050.h", 11, "check-undefined") in codes
    assert ("Core/Src/main.c", 10, "check-placeholder") in codes
    assert ("Core/Src/mpu6050.c", 5, "check-commented-code") in codes
    assert ("Core/Src/mpu6050.c", 2, "check-ascii") in codes
    assert all(d.severity == "error" and d.tool == checks.TOOL for d in findings)
    # Templates are never blamed, and template text outside USER CODE is not the model's.
    assert not any(d.file == "Core/Src/system_stm32f4xx.c" for d in findings)
    assert not any(d.line == 22 for d in findings if d.file == "Core/Src/main.c")
    # Most consequential first: the repair loop only sees the first few.
    assert findings[0].code == "check-unreachable"


def test_a_finished_project_passes():
    assert checks.check_sources(_project(main=MAIN_WIRED, driver=GOOD_DRIVER)) == []


def test_a_mismatched_definition_is_reported_where_it_is_defined():
    driver = GOOD_DRIVER.replace(
        "HAL_StatusTypeDef MPU6050_Init(void)", "int32_t MPU6050_Init(void)"
    )
    findings = checks.check_sources(_project(main=MAIN_WIRED, driver=driver))

    assert _codes(findings) == [("Core/Src/mpu6050.c", 2, "check-signature")]
    assert "HAL_StatusTypeDef MPU6050_Init(void)" in findings[0].message


def test_parameter_names_do_not_count_as_a_mismatch():
    driver = GOOD_DRIVER.replace("MPU6050_Data_t *data)", "MPU6050_Data_t* out)").replace(
        "data->", "out->"
    )
    assert checks.check_sources(_project(main=MAIN_WIRED, driver=driver)) == []


def test_a_module_reached_only_from_a_hal_callback_is_reachable():
    driver = GOOD_DRIVER + (
        "void HAL_SPI_RxCpltCallback(SPI_HandleTypeDef *hspi)\n{\n  (void)hspi;\n}\n"
    )
    main = MAIN_WIRED
    assert checks.check_sources(_project(main=main, driver=driver)) == []


def test_calls_ignore_comments_and_strings():
    text = 'void f(void) { g(); /* h(); */ printf("k()"); }'
    assert checks.calls(text) == {"f", "g", "printf"}


# --------------------------------------------------------------------------
# The repair guard
# --------------------------------------------------------------------------


def _err(message: str = "'spi_bus_select' undeclared") -> list[Diagnostic]:
    return [Diagnostic(file="Core/Src/mpu6050.c", line=5, message=message)]


def test_deleting_a_call_is_rejected_unless_the_error_named_it():
    original = "void f(void)\n{\n  MPU6050_Init();\n  spi_bus_select();\n}\n"

    problems, _ = guard_patch("Core/Src/x.c", original, "void f(void)\n{\n}\n", _err(), [])
    assert problems and "`MPU6050_Init`" in problems[0]
    assert "spi_bus_select" not in problems[0]  # replacing that one *is* the fix


def test_a_removal_with_a_reason_is_accepted_and_reported():
    from app.agents.repair import Removal

    original = "void f(void)\n{\n  MPU6050_Init();\n}\n"
    removed = [Removal(name="MPU6050_Init", reason="already called once from main()")]

    problems, accepted = guard_patch("Core/Src/x.c", original, "void f(void)\n{\n}\n", [], removed)
    assert problems == []
    assert accepted == ["`MPU6050_Init`: already called once from main()"]


def test_commenting_out_is_rejected():
    original = "void f(void)\n{\n  g();\n}\n"
    patched = "void f(void)\n{\n  /* g(); */\n}\n"

    problems, _ = guard_patch("Core/Src/x.c", original, patched, [], [])
    assert any("`g`" in p for p in problems)
    assert any("placeholder" in p for p in problems)


class SequenceLLM:
    def __init__(self, *replies: dict):
        self.replies = [json.dumps(reply) for reply in replies]
        self.calls: list[list[dict]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        return self.replies[min(len(self.calls), len(self.replies)) - 1]


@pytest.fixture
def ws(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_root", str(tmp_path / "ws"))
    workspace.ensure_workspace("q1")
    return "q1"


def test_a_rejected_patch_gets_one_more_try_with_the_reason(ws):
    path = "Core/Src/app.c"
    source = "\n".join(
        ["#include \"main.h\"", "void run(void)", "{", "  MPU6050_Init();", "  foo = 1;", "}"]
    )
    workspace.write_file(ws, path, source)
    bundle = FirmwareBundle(files=[SourceFile(path=path, contents=source)])
    failed = BuildResult(
        status="failed",
        diagnostics=[Diagnostic(file=path, line=5, message="'foo' undeclared")],
    )
    llm = SequenceLLM(
        {"path": path, "edits": [{"start_line": 4, "end_line": 5, "replacement": ""}]},
        {
            "path": path,
            "edits": [{"start_line": 5, "end_line": 5, "replacement": "  int foo = 1;"}],
        },
    )

    updated, _warnings, report = asyncio.run(repair_firmware(bundle, failed, project_id=ws, llm=llm))

    assert len(llm.calls) == 2
    assert "Rejected" in llm.calls[1][-1]["content"]
    assert "MPU6050_Init" in llm.calls[1][-1]["content"]
    assert "MPU6050_Init();" in workspace.read_file(ws, path)
    assert "int foo = 1;" in updated.file(path).contents
    assert report["files"] == {path: 1}
    assert any("without fixing the cause" in r for r in report["rejected"])


# --------------------------------------------------------------------------
# Status: compiled but unfinished
# --------------------------------------------------------------------------

OK_PAYLOAD = {
    "status": "ok",
    "exit_code": 0,
    "duration_ms": 10,
    "toolchain": "arm-none-eabi-gcc 14.2.1",
    "command": "make -j4",
    "log": "",
    "artifacts": {"elf": "build/app.elf"},
    "size_output": "",
}


def _ok_client() -> BuilderClient:
    return BuilderClient(
        base_url="http://builder:9000",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=OK_PAYLOAD)),
    )


async def _no_artifacts(*args, **kwargs):
    return None


@pytest.mark.parametrize(
    ("main", "driver", "status"),
    [(MAIN_HOLLOW, DRIVER, BUILD_INCOMPLETE), (MAIN_WIRED, GOOD_DRIVER, BUILD_OK)],
)
def test_a_clean_compile_is_held_to_the_checker(ws, monkeypatch, main, driver, status):
    monkeypatch.setattr(build_module, "record_artifacts", _no_artifacts)
    for path, text in _project(main=main, driver=driver).items():
        workspace.write_file(ws, path, text)

    async def go():
        client = _ok_client()
        try:
            return await build_module.run_build(ws, CubeMXPlan(mcu="STM32F407VGT6"), client=client)
        finally:
            await client.aclose()

    result = asyncio.run(go())

    assert result.status == status
    if status == BUILD_INCOMPLETE:
        bundle = FirmwareBundle(files=[SourceFile(path="Core/Src/mpu6050.c")])
        assert should_repair(result, bundle, attempt=1)
        # main.c is repairable for checker findings even when no step wrote it.
        assert any(d.file == "Core/Src/main.c" for d in repairable_errors(result, bundle))


# --------------------------------------------------------------------------
# Chip select
# --------------------------------------------------------------------------


def _spi_data() -> DeviceData:
    return DeviceData(
        part="stm32f407xx",
        source="fixture",
        pins={
            "PA0": {},
            "PA4": {"SPI1_NSS": 5},
            "PA5": {"SPI1_SCK": 5},
            "PA6": {"SPI1_MISO": 5},
            "PA7": {"SPI1_MOSI": 5},
        },
        instances=["SPI1"],
    )


def _spi_plan(**parameters: str) -> CubeMXPlan:
    return CubeMXPlan(
        mcu="STM32F407VGT6",
        peripherals=[
            PeripheralConfig(peripheral="SPI1", mode="master_full_duplex", parameters=parameters)
        ],
    )


def test_software_nss_gets_a_chip_select_on_the_nss_pin_idling_high():
    plan = _spi_plan()
    result = complete_plan(plan, _spi_data())

    chip_select = [pin for pin in plan.pins if pin.signal == "SPI1_CS"]
    assert result.errors == []
    assert [(pin.pin, pin.mode) for pin in chip_select] == [("PA4", "output")]
    assert "SPI1_CS=PA4" in result.selected_pins
    text = gpio_init(plan, [])
    assert "HAL_GPIO_WritePin(GPIOA, GPIO_PIN_4, GPIO_PIN_SET);" in text


def test_hardware_nss_or_an_existing_chip_select_adds_nothing():
    hard = _spi_plan(NSS="SPI_NSS_HARD_OUTPUT")
    complete_plan(hard, _spi_data())
    assert not any(pin.signal == "SPI1_CS" for pin in hard.pins)

    given = _spi_plan()
    given.pins.append(PinAssignment(pin="PA0", signal="IMU_CS", peripheral="SPI1", mode="output"))
    complete_plan(given, _spi_data())
    assert [pin.pin for pin in given.pins if pin.mode == "output"] == ["PA0"]
