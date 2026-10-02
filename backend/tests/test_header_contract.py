"""A source against its own header, offline.

The I2C MPU6050 run failed eleven builds on one error: `mpu6050.h` declared
`MPU6050_Init(I2C_HandleTypeDef *hi2c)` (and main.c called it that way),
`mpu6050.c` defined `MPU6050_Init(void)`, and two more declared functions
were never defined. The fixtures below are cut from that project.
"""

import asyncio
import json
from pathlib import Path

import pytest

from app.agents.firmware import generate_firmware
from app.agents.repair import repair_firmware
from app.build import workspace
from app.codegen import checks
from app.core.config import settings
from app.orchestrator.contracts import (
    Architecture,
    BuildResult,
    CubeMXPlan,
    Diagnostic,
    FirmwareBundle,
    HardwareFindings,
    ImplementationStep,
    Requirements,
    SourceFile,
)

HEADER_PATH = "Core/Inc/mpu6050.h"
SOURCE_PATH = "Core/Src/mpu6050.c"
HEADER = """#ifndef MPU6050_H
#define MPU6050_H
#include "main.h"
typedef struct { int16_t ax; } MPU6050_Data_t;
HAL_StatusTypeDef MPU6050_Init(I2C_HandleTypeDef *hi2c);
HAL_StatusTypeDef MPU6050_ReadData_DMA(I2C_HandleTypeDef *hi2c, MPU6050_Data_t *data);
#endif
"""
SOURCE = """#include "mpu6050.h"
extern I2C_HandleTypeDef hi2c1;

HAL_StatusTypeDef MPU6050_Init(void)
{
  uint8_t check;
  return HAL_I2C_Mem_Read(&hi2c1, 0xD0, 0x75, 1, &check, 1, 100);
}

HAL_StatusTypeDef MPU6050_Read_All_DMA(uint8_t *pData)
{
  return HAL_I2C_Mem_Read_DMA(&hi2c1, 0xD0, 0x3B, 1, pData, 14);
}
"""
GOOD_SOURCE = """#include "mpu6050.h"
extern I2C_HandleTypeDef hi2c1;

HAL_StatusTypeDef MPU6050_Init(I2C_HandleTypeDef *hi2c)
{
  uint8_t check;
  return HAL_I2C_Mem_Read(hi2c, 0xD0, 0x75, 1, &check, 1, 100);
}

HAL_StatusTypeDef MPU6050_ReadData_DMA(I2C_HandleTypeDef *hi2c, MPU6050_Data_t *data)
{
  return HAL_I2C_Mem_Read_DMA(hi2c, 0xD0, 0x3B, 1, (uint8_t *)data, 14);
}
"""


def test_the_contract_names_every_break():
    problems = checks.header_contract(HEADER_PATH, HEADER, SOURCE_PATH, SOURCE)
    assert len(problems) == 2
    assert "MPU6050_Init(I2C_HandleTypeDef *hi2c)" in problems[0]
    assert "use the header's signature" in problems[0]
    assert "MPU6050_ReadData_DMA" in problems[1] and "does not define it" in problems[1]
    assert checks.header_contract(HEADER_PATH, HEADER, SOURCE_PATH, GOOD_SOURCE) == []


def test_a_void_definition_takes_the_header_parameters():
    aligned, fixed = checks.align_definitions(HEADER_PATH, HEADER, SOURCE)
    assert fixed == ["MPU6050_Init"]
    assert "HAL_StatusTypeDef MPU6050_Init(I2C_HandleTypeDef *hi2c)\n{" in aligned
    # A definition with parameters of its own is the model's to fix.
    assert "MPU6050_Read_All_DMA(uint8_t *pData)" in aligned


def test_a_different_return_type_is_not_aligned():
    source = SOURCE.replace("HAL_StatusTypeDef MPU6050_Init(void)", "void MPU6050_Init(void)")
    assert checks.align_definitions(HEADER_PATH, HEADER, source) == (source, [])


def test_conflicting_types_carry_the_declaration():
    error = Diagnostic(
        file=SOURCE_PATH,
        line=4,
        message="conflicting types for 'MPU6050_Init'; have 'HAL_StatusTypeDef(void)'",
    )
    [explained] = checks.explain_conflicts([error], {HEADER_PATH: HEADER})
    assert f"declared in {HEADER_PATH}:5" in explained.message
    assert "MPU6050_Init(I2C_HandleTypeDef *hi2c)" in explained.message


class ScriptedLLM:
    def __init__(self, *replies: dict):
        self.replies = [json.dumps(reply) for reply in replies]
        self.calls: list[list[dict]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        return self.replies[min(len(self.calls), len(self.replies)) - 1]


def _generate(*replies: dict):
    architecture = Architecture(
        implementation_order=[
            ImplementationStep(order=1, title="Driver", files=[HEADER_PATH, SOURCE_PATH])
        ]
    )
    llm = ScriptedLLM(*replies)
    bundle, warnings = asyncio.run(
        generate_firmware(
            Requirements(), HardwareFindings(), architecture, CubeMXPlan(), llm=llm
        )
    )
    return llm, bundle, warnings


def test_a_source_that_breaks_its_header_is_sent_back_once():
    llm, bundle, warnings = _generate(
        {"path": HEADER_PATH, "contents": HEADER},
        {"path": SOURCE_PATH, "contents": SOURCE},
        {"path": SOURCE_PATH, "contents": GOOD_SOURCE},
    )
    assert len(llm.calls) == 3
    feedback = llm.calls[2][-1]["content"]
    assert "MPU6050_ReadData_DMA" in feedback and HEADER_PATH in feedback
    assert bundle.file(SOURCE_PATH).contents == GOOD_SOURCE
    assert not any("does not define" in w for w in warnings)


def test_what_the_retry_leaves_is_aligned_or_reported():
    llm, bundle, warnings = _generate(
        {"path": HEADER_PATH, "contents": HEADER},
        {"path": SOURCE_PATH, "contents": SOURCE},
    )
    assert len(llm.calls) == 3  # the retry got the same answer
    assert "MPU6050_Init(I2C_HandleTypeDef *hi2c)" in bundle.file(SOURCE_PATH).contents
    assert any("taken from Core/Inc/mpu6050.h" in w for w in warnings)
    assert any("MPU6050_ReadData_DMA" in w and "does not define" in w for w in warnings)


class NoLLM:
    async def chat(self, messages, **kwargs):
        raise AssertionError("the model should not be asked")


@pytest.fixture
def ws(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_root", str(tmp_path / "ws"))
    workspace.ensure_workspace("hc1")
    return "hc1"


def test_repair_settles_a_void_clash_without_the_model(ws):
    workspace.write_file(ws, HEADER_PATH, HEADER)
    workspace.write_file(ws, SOURCE_PATH, SOURCE)
    bundle = FirmwareBundle(
        files=[
            SourceFile(path=HEADER_PATH, contents=HEADER),
            SourceFile(path=SOURCE_PATH, contents=SOURCE),
        ]
    )
    failed = BuildResult(
        status="failed",
        diagnostics=[
            Diagnostic(
                file=SOURCE_PATH,
                line=4,
                message="conflicting types for 'MPU6050_Init'; have 'HAL_StatusTypeDef(void)'",
            )
        ],
    )
    updated, _warnings, report = asyncio.run(
        repair_firmware(bundle, failed, project_id=ws, llm=NoLLM())
    )
    on_disk = workspace.read_file(ws, SOURCE_PATH)
    assert "MPU6050_Init(I2C_HandleTypeDef *hi2c)" in on_disk
    assert updated.file(SOURCE_PATH).contents == on_disk
    assert report["files"] == {SOURCE_PATH: 1}
    assert any("taken from" in note for note in report["notes"])
