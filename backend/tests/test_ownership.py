"""Generated files stay off what the CubeMX scaffold owns, offline.

The SPI run that failed eleven builds in a row planned `spi1.c`/`spi1.h`:
the model rewrote MX_SPI1_Init and the MSP hooks, lost the head of
HAL_SPI_MspInit (leaving statements at file scope), defined the handles a
second time, and `mpu6050.c` included the copy. Every case below is a piece
of that project.
"""

import asyncio
import json

from app.agents.architecture import _enforce_ownership
from app.agents.firmware import _SYSTEM_PROMPT, generate_firmware
from app.codegen.ownership import (
    drop_includes,
    scaffold_file,
    scaffold_owned_path,
    strip_owned,
)
from app.orchestrator.contracts import (
    Architecture,
    CubeMXPlan,
    HardwareFindings,
    ImplementationStep,
    Module,
    Requirements,
)

SPI1_C = """#include "main.h"
#include "spi1.h"

SPI_HandleTypeDef hspi1;
DMA_HandleTypeDef hdma_spi1_rx;

void MX_SPI1_Init(void)
{
  hspi1.Instance = SPI1;
  if (HAL_SPI_Init(&hspi1) != HAL_OK)
  {
    Error_Handler();
  }
}

/* USER CODE BEGIN SPI1_MspInit 0 */
    __HAL_RCC_SPI1_CLK_ENABLE();
    {
    HAL_GPIO_WritePin(SPI1_CS_GPIO_Port, SPI1_CS_Pin, GPIO_PIN_SET);
    __HAL_LINKDMA(spiHandle, hdmarx, hdma_spi1_rx);
  }
}

void HAL_SPI_MspDeInit(SPI_HandleTypeDef* spiHandle)
{
  if(spiHandle->Instance==SPI1)
  {
    __HAL_RCC_SPI1_CLK_DISABLE();
  }
}
"""

SPI1_H = """#ifndef INC_SPI1_H_
#define INC_SPI1_H_
#ifdef __cplusplus
extern "C" {
#endif
#include "main.h"
void MX_SPI1_Init(void);
#ifdef __cplusplus
}
#endif
#endif /* INC_SPI1_H_ */
"""

SENSOR_C = """#include "spi1.h"
#include "main.h"
#include "mpu6050.h"
#include <string.h>

SPI_HandleTypeDef hspi1;
static const uint8_t table[] = {1, 2};

void MPU6050_Init(void)
{
  HAL_GPIO_WritePin(SPI1_CS_GPIO_Port, SPI1_CS_Pin, GPIO_PIN_SET);
}

void HAL_SPI_MspInit(SPI_HandleTypeDef *hspi)
{
  (void)hspi;
}

void HAL_SPI_TxRxCpltCallback(SPI_HandleTypeDef *hspi)
{
  (void)hspi;
}
"""


def test_peripheral_and_scaffold_files_are_owned():
    for path in [
        "Core/Src/spi1.c",
        "Core/Inc/spi1.h",
        "Core/Src/i2c1.c",
        "Core/Src/gpio.c",
        "Core/Src/dma.c",
        "Core/Src/usart2.c",
        "Core/Src/stm32f4xx_it.c",
        "Core/Src/stm32f4xx_hal_msp.c",
    ]:
        assert scaffold_owned_path(path), path
    for path in [
        "Core/Src/main.c",
        "Core/Inc/main.h",
        "Core/Src/mpu6050.c",
        "Core/Src/spi_bus.c",
        "Core/Src/uart.c",
        "Core/Src/spi.c",
    ]:
        assert not scaffold_owned_path(path), path
    assert scaffold_file("Core/Inc/stm32f4xx_it.h")
    assert not scaffold_file("Core/Inc/spi1.h")


def test_a_cubemx_copy_is_dropped_whole():
    contents, removed = strip_owned("Core/Src/spi_bus.c", SPI1_C)
    assert contents is None
    assert {"MX_SPI1_Init", "HAL_SPI_MspDeInit", "hspi1", "hdma_spi1_rx"} <= set(removed)

    contents, removed = strip_owned("Core/Inc/spi_bus.h", SPI1_H)
    assert contents is None
    assert removed == ["MX_SPI1_Init"]


def test_a_driver_keeps_its_code_and_loses_the_scaffold_parts():
    contents, removed = strip_owned("Core/Src/mpu6050.c", SENSOR_C)
    assert contents is not None
    assert set(removed) == {"HAL_SPI_MspInit", "hspi1"}
    assert "extern SPI_HandleTypeDef hspi1;" in contents
    assert "void MPU6050_Init(void)" in contents
    assert "HAL_SPI_TxRxCpltCallback" in contents
    assert "HAL_SPI_MspInit" not in contents
    assert "table[] = {1, 2};" in contents


def test_main_and_clean_files_are_left_alone():
    assert strip_owned("Core/Src/main.c", SPI1_C) == (SPI1_C, [])
    clean = "#include \"main.h\"\nvoid run(void)\n{\n}\n"
    assert strip_owned("Core/Src/app.c", clean) == (clean, [])


def test_includes_of_dropped_headers_are_removed():
    cleaned = drop_includes(SENSOR_C, {"Core/Inc/spi1.h"})
    assert '#include "spi1.h"' not in cleaned
    assert '#include "mpu6050.h"' in cleaned
    assert "#include <string.h>" in cleaned


def test_the_plan_loses_scaffold_owned_files():
    architecture = Architecture(
        modules=[
            Module(name="spi1", path="Core/Src/spi1.c"),
            Module(name="mpu6050", path="Core/Src/mpu6050.c"),
        ],
        file_tree=["Core/Src/spi1.c", "Core/Inc/spi1.h", "Core/Src/mpu6050.c"],
        implementation_order=[
            ImplementationStep(
                order=1, title="SPI", files=["Core/Inc/spi1.h", "Core/Src/spi1.c"]
            ),
            ImplementationStep(
                order=2, title="Driver", files=["Core/Inc/mpu6050.h", "Core/Src/mpu6050.c"]
            ),
        ],
    )
    planned, warnings = _enforce_ownership(architecture)
    assert planned.file_tree == ["Core/Src/mpu6050.c"]
    assert [m.name for m in planned.modules] == ["mpu6050"]
    assert [(s.order, s.title) for s in planned.implementation_order] == [(1, "Driver")]
    assert len(warnings) == 2


def test_the_firmware_prompt_names_the_scaffold_owned_code():
    assert "MX_*_Init" in _SYSTEM_PROMPT
    assert "spi1.c" in _SYSTEM_PROMPT
    assert '"path": "Core/Src/example.c"' in _SYSTEM_PROMPT


class PathLLM:
    """Answers each file prompt with the reply registered for its path."""

    def __init__(self, replies: dict[str, str]):
        self.replies = replies
        self.paths: list[str] = []

    async def chat(self, messages, **kwargs):
        target = messages[-1]["content"].split("\n", 1)[0]
        for path, contents in self.replies.items():
            if target == f"# Target File: `{path}`":
                self.paths.append(path)
                return json.dumps({"path": path, "contents": contents})
        raise AssertionError(f"unexpected prompt: {target}")


def test_generation_skips_and_strips_scaffold_code():
    architecture = Architecture(
        implementation_order=[
            ImplementationStep(
                order=1,
                title="Bus",
                files=["Core/Src/gpio.c", "Core/Inc/spi_bus.h", "Core/Src/spi_bus.c"],
            ),
            ImplementationStep(order=2, title="Driver", files=["Core/Src/mpu6050.c"]),
        ]
    )
    llm = PathLLM(
        {
            "Core/Inc/spi_bus.h": SPI1_H,
            "Core/Src/spi_bus.c": SPI1_C,
            "Core/Src/mpu6050.c": SENSOR_C.replace("spi1.h", "spi_bus.h"),
        }
    )
    bundle, warnings = asyncio.run(
        generate_firmware(
            Requirements(), HardwareFindings(), architecture, CubeMXPlan(), llm=llm
        )
    )
    assert "Core/Src/gpio.c" not in llm.paths
    assert bundle.paths == ["Core/Src/mpu6050.c"]
    driver = bundle.files[0].contents
    assert '#include "spi_bus.h"' not in driver
    assert "extern SPI_HandleTypeDef hspi1;" in driver
    assert any("gpio.c: owned by the CubeMX scaffold" in w for w in warnings)
    assert any("spi_bus.c: only re-implemented scaffold code" in w for w in warnings)


def test_calls_and_wrapped_prototypes_are_told_apart():
    source = (
        '#include "main.h"\n'
        "void Error_Handler(void);\n"
        "void run(void)\n{\n  if (bad) {\n    Error_Handler();\n  }\n}\n"
    )
    contents, removed = strip_owned("Core/Src/app.c", source)
    assert removed == ["Error_Handler"]
    assert "    Error_Handler();" in contents

    header = (
        '#ifdef __cplusplus\nextern "C" {\n#endif\n'
        "void MX_SPI1_Init(void);\nvoid keep(void);\n"
        "#ifdef __cplusplus\n}\n#endif\n"
    )
    contents, removed = strip_owned("Core/Inc/app.h", header)
    assert removed == ["MX_SPI1_Init"]
    assert "void keep(void);" in contents
