"""Project generation (M4, P2), tested without a compiler.

The compiler is the real gate -- `make scaffold` builds a generated project
for an actual board -- but it only tells us *that* something is wrong. These
tests pin down the decisions that are silent when they are wrong: a wait state
for a clock the plan asked for, a peripheral clock enabled before its pins, an
alternate-function number that was never looked up, a HAL module compiled but
not enabled in the configuration header.

Each one is a mistake a model makes confidently and a compiler accepts.
"""

import contextlib
import tempfile
from pathlib import Path

import pytest

from app.build import workspace
from app.codegen import halconf, peripherals, sdk
from app.codegen.devices import device_for
from app.codegen.errors import CodegenError
from app.codegen.render import merge_user_code, render, user_regions
from app.codegen.scaffold import scaffold_project
from app.core.config import settings
from app.orchestrator.contracts import (
    ClockPlan,
    CubeMXPlan,
    DmaConfig,
    PeripheralConfig,
    PinAssignment,
)

PROJECT = "scaf1"

FAKE_MODULES = (
    "cortex",
    "dma",
    "dma_ex",
    "exti",
    "flash",
    "flash_ex",
    "gpio",
    "i2c",
    "i2c_ex",
    "pwr",
    "pwr_ex",
    "rcc",
    "rcc_ex",
    "spi",
    "tim",
    "tim_ex",
    "uart",
    "usart",
)

# Shaped like ST's own template: a mix of enabled and commented-out modules,
# and a crystal frequency for a board that is not ours.
FAKE_HAL_CONF = """/* stm32f4xx_hal_conf_template.h */
#ifndef __STM32F4xx_HAL_CONF_H
#define __STM32F4xx_HAL_CONF_H

#define HAL_MODULE_ENABLED
#define HAL_ADC_MODULE_ENABLED
#define HAL_CRYP_MODULE_ENABLED
#define HAL_GPIO_MODULE_ENABLED
#define HAL_DMA_MODULE_ENABLED
#define HAL_RCC_MODULE_ENABLED
#define HAL_FLASH_MODULE_ENABLED
#define HAL_PWR_MODULE_ENABLED
#define HAL_CORTEX_MODULE_ENABLED
#define HAL_EXTI_MODULE_ENABLED
/* #define HAL_SPI_MODULE_ENABLED   */
/* #define HAL_I2C_MODULE_ENABLED   */
/* #define HAL_UART_MODULE_ENABLED   */
/* #define HAL_USART_MODULE_ENABLED   */
/* #define HAL_TIM_MODULE_ENABLED   */

#if !defined  (HSE_VALUE)
  #define HSE_VALUE    25000000U /*!< external oscillator in Hz */
#endif /* HSE_VALUE */

#endif /* __STM32F4xx_HAL_CONF_H */
"""


def write(path: Path, text: str = "/* vendor */\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_sdk(root: Path) -> None:
    write(root / "VERSION", "stm32f4xx_hal_driver refs/tags/v1.8.3\n")
    hal = root / sdk.HAL_DIR
    write(hal / "Inc/stm32f4xx_hal.h")
    write(hal / f"Inc/{halconf.TEMPLATE_NAME}", FAKE_HAL_CONF)
    write(hal / "Src/stm32f4xx_hal.c")
    for module in FAKE_MODULES:
        write(hal / f"Src/stm32f4xx_hal_{module}.c")

    device = root / sdk.DEVICE_DIR
    write(device / "Include/stm32f4xx.h")
    write(device / "Include/system_stm32f4xx.h")
    write(device / "Include/stm32f407xx.h")
    write(device / "Source/Templates/system_stm32f4xx.c")
    write(device / "Source/Templates/gcc/startup_stm32f407xx.s")

    write(root / sdk.CMSIS_DIR / "Include/core_cm4.h")


@contextlib.contextmanager
def sandbox():
    previous = (settings.workspace_root, settings.cube_sdk_root)
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        settings.workspace_root = str(base / "workspaces")
        settings.cube_sdk_root = str(base / "sdk")
        make_sdk(Path(settings.cube_sdk_root))
        try:
            yield base
        finally:
            settings.workspace_root, settings.cube_sdk_root = previous


def discovery_plan(**overrides) -> CubeMXPlan:
    """The plan the CubeMX agent is expected to produce in P3."""
    plan = CubeMXPlan(
        mcu="STM32F407VGT6",
        board="STM32F4 Discovery",
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
        ),
        pins=[
            PinAssignment(pin="PD12", signal="LED_GREEN", mode="output", speed="low"),
            PinAssignment(pin="PA2", signal="USART2_TX", peripheral="USART2", alternate=7),
            PinAssignment(pin="PA3", signal="USART2_RX", peripheral="USART2", alternate=7),
            PinAssignment(pin="PB8", signal="I2C1_SCL", peripheral="I2C1", alternate=4),
            PinAssignment(pin="PB9", signal="I2C1_SDA", peripheral="I2C1", alternate=4),
        ],
        peripherals=[
            PeripheralConfig(
                peripheral="USART2",
                parameters={"BaudRate": "9600"},
                nvic_priority=5,
            ),
            PeripheralConfig(peripheral="I2C1", parameters={"ClockSpeed": "400000"}),
        ],
        validated=True,
    )
    for key, value in overrides.items():
        setattr(plan, key, value)
    return plan


def generated(project_id: str, relative: str) -> str:
    return workspace.read_file(project_id, relative)


# --------------------------------------------------------------------------
# The project as a whole
# --------------------------------------------------------------------------


def test_every_file_the_makefile_compiles_is_in_the_project() -> None:
    with sandbox():
        result = scaffold_project(PROJECT, discovery_plan())
        makefile = generated(PROJECT, "Makefile")

        for relative in result.sources:
            assert workspace.safe_join(PROJECT, relative).is_file(), relative
            assert relative in makefile
        # The drivers are not in git, so a project that references them
        # without carrying them is a download that cannot be built.
        assert f"{sdk.HAL_DIR}/Src/stm32f4xx_hal_i2c.c" in result.sources
        assert "Core/Startup/startup_stm32f407xx.s" in result.sources
        assert "-ICore/Inc" in makefile
        assert "-DSTM32F407xx -DUSE_HAL_DRIVER" in makefile


def test_the_linker_script_describes_this_part_and_not_another() -> None:
    with sandbox():
        scaffold_project(PROJECT, discovery_plan())
        script = generated(PROJECT, "STM32F407xx_FLASH.ld")

        assert "LENGTH = 1024K" in script  # flash
        assert "LENGTH = 128K" in script  # sram
        assert "CCMRAM (xrw) : ORIGIN = 0x10000000" in script
        # The names ST's startup file jumps to. Renaming one links fine and
        # crashes on the first initialised global.
        for symbol in ("_estack", "_sidata", "_sdata", "_edata", "_sbss", "_ebss"):
            assert symbol in script


def test_an_mcu_with_no_table_is_refused_rather_than_approximated() -> None:
    with pytest.raises(CodegenError) as error:
        device_for("STM32H743ZI")
    assert "stm32f407xx" in str(error.value)


def test_a_plan_nobody_checked_says_so_in_the_report() -> None:
    with sandbox():
        result = scaffold_project(PROJECT, discovery_plan(validated=False))

    assert any("never checked against" in warning for warning in result.warnings)


# --------------------------------------------------------------------------
# Clock tree
# --------------------------------------------------------------------------


def test_the_clock_numbers_in_the_plan_are_the_ones_configured() -> None:
    with sandbox():
        scaffold_project(PROJECT, discovery_plan())
        main = generated(PROJECT, "Core/Src/main.c")

        assert "RCC_OscInitStruct.PLL.PLLM = 8;" in main
        assert "RCC_OscInitStruct.PLL.PLLN = 336;" in main
        assert "RCC_OscInitStruct.PLL.PLLP = RCC_PLLP_DIV2;" in main
        assert "RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV4;" in main
        assert "RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV2;" in main
        # 168 MHz needs five wait states. Too few is a chip that runs for a
        # while and then reads garbage from flash.
        assert "FLASH_LATENCY_5" in main


def test_wait_states_follow_the_frequency() -> None:
    assert peripherals.flash_latency(16_000_000) == "FLASH_LATENCY_0"
    assert peripherals.flash_latency(84_000_000) == "FLASH_LATENCY_2"
    assert peripherals.flash_latency(168_000_000) == "FLASH_LATENCY_5"


def test_a_plan_with_no_clock_still_produces_a_board_that_boots() -> None:
    with sandbox():
        scaffold_project(PROJECT, discovery_plan(clock=ClockPlan()))
        main = generated(PROJECT, "Core/Src/main.c")

        assert "RCC_OSCILLATORTYPE_HSI" in main
        assert "RCC_OscInitStruct.PLL.PLLState = RCC_PLL_NONE;" in main
        assert "RCC_SYSCLKSOURCE_HSI" in main
        assert "FLASH_LATENCY_0" in main


def test_a_frequency_the_part_cannot_reach_is_reported() -> None:
    fast = ClockPlan(source="hsi", sysclk_hz=200_000_000, hclk_hz=200_000_000)
    with sandbox():
        result = scaffold_project(PROJECT, discovery_plan(clock=fast))

    assert any("ceiling" in warning for warning in result.warnings)


def test_hse_without_a_crystal_frequency_is_refused() -> None:
    with sandbox(), pytest.raises(CodegenError) as error:
        scaffold_project(PROJECT, discovery_plan(clock=ClockPlan(source="hse")))
    assert "hse_hz" in str(error.value)


# --------------------------------------------------------------------------
# Pins and peripherals
# --------------------------------------------------------------------------


def test_a_peripheral_pin_carries_its_alternate_function_number() -> None:
    with sandbox():
        scaffold_project(PROJECT, discovery_plan())
        msp = generated(PROJECT, "Core/Src/stm32f4xx_hal_msp.c")

        assert "GPIO_InitStruct.Alternate = GPIO_AF7_USART2;" in msp
        assert "GPIO_InitStruct.Pin = GPIO_PIN_2|GPIO_PIN_3;" in msp


def test_a_pin_whose_alternate_function_was_never_looked_up_stops_generation() -> None:
    plan = discovery_plan()
    plan.pins[1].alternate = None
    with sandbox(), pytest.raises(CodegenError) as error:
        scaffold_project(PROJECT, plan)
    # The plan has to be validated against the pin table first. Guessing AF7
    # because it is usually AF7 is how a UART ends up silently dead.
    assert "alternate-function" in str(error.value)


def test_an_i2c_pin_is_open_drain() -> None:
    with sandbox():
        scaffold_project(PROJECT, discovery_plan())
        msp = generated(PROJECT, "Core/Src/stm32f4xx_hal_msp.c")

        # Push-pull SDA and SCL fight every other device on the bus, and the
        # code compiles either way.
        i2c_block = msp.split("i2cHandle->Instance == I2C1", 1)[1]
        assert "GPIO_MODE_AF_OD" in i2c_block


def test_a_peripheral_clock_is_enabled_before_its_pins_are_touched() -> None:
    with sandbox():
        scaffold_project(PROJECT, discovery_plan())
        msp = generated(PROJECT, "Core/Src/stm32f4xx_hal_msp.c")

        clock = msp.index("__HAL_RCC_USART2_CLK_ENABLE();")
        gpio = msp.index("__HAL_RCC_GPIOA_CLK_ENABLE();")
        init = msp.index("HAL_GPIO_Init(GPIOA")
        # Writes to a peripheral with its clock off are discarded in silence.
        assert clock < gpio < init


def test_every_port_in_the_plan_has_its_clock_enabled() -> None:
    with sandbox():
        scaffold_project(PROJECT, discovery_plan())
        main = generated(PROJECT, "Core/Src/main.c")

        assert "__HAL_RCC_GPIOD_CLK_ENABLE();" in main
        assert "HAL_GPIO_WritePin(GPIOD, GPIO_PIN_12, GPIO_PIN_RESET);" in main
        assert "GPIO_InitStruct.Mode = GPIO_MODE_OUTPUT_PP;" in main


def test_a_named_pin_becomes_a_macro_the_firmware_can_use() -> None:
    with sandbox():
        scaffold_project(PROJECT, discovery_plan())
        header = generated(PROJECT, "Core/Inc/main.h")

        assert "#define LED_GREEN_Pin GPIO_PIN_12" in header
        assert "#define LED_GREEN_GPIO_Port GPIOD" in header


def test_the_short_form_of_a_hal_option_is_completed_not_pasted() -> None:
    assert peripherals.enum_value("SPI_BAUDRATEPRESCALER_16", "8") == "SPI_BAUDRATEPRESCALER_8"
    assert peripherals.enum_value("I2C_DUTYCYCLE_2", "16_9") == "I2C_DUTYCYCLE_16_9"
    assert peripherals.enum_value("UART_MODE_TX_RX", "TX") == "UART_MODE_TX"
    assert peripherals.enum_value("SPI_MODE_MASTER", "slave") == "SPI_MODE_SLAVE"
    # Numbers stay numbers, and a macro spelled out in full is left alone.
    assert peripherals.enum_value("115200", "9600") == "9600"
    assert (
        peripherals.enum_value("UART_WORDLENGTH_8B", "UART_WORDLENGTH_9B")
        == "UART_WORDLENGTH_9B"
    )


def test_a_parameter_this_generator_ignores_is_reported() -> None:
    plan = discovery_plan()
    plan.peripherals[0].parameters["Invented"] = "true"
    with sandbox():
        result = scaffold_project(PROJECT, plan)

    assert any("Invented" in warning for warning in result.warnings)


def test_a_peripheral_with_no_template_is_left_out_and_named() -> None:
    plan = discovery_plan()
    plan.peripherals.append(PeripheralConfig(peripheral="ADC1"))
    with sandbox():
        result = scaffold_project(PROJECT, plan)
        main = generated(PROJECT, "Core/Src/main.c")

    assert "ADC1" not in result.configured
    assert "MX_ADC1_Init" not in main
    assert any("ADC1" in warning for warning in result.warnings)


def test_dma_the_plan_asked_for_is_emitted() -> None:
    plan = discovery_plan()
    plan.peripherals[1].dma = [
        DmaConfig(
            request="I2C1_RX",
            stream="DMA1_Stream0",
            channel=1,
            direction="peripheral_to_memory",
            nvic_priority=5,
        )
    ]
    with sandbox():
        result = scaffold_project(PROJECT, plan)
        main = generated(PROJECT, "Core/Src/main.c")
        msp = generated(PROJECT, "Core/Src/stm32f4xx_hal_msp.c")
        handlers = generated(PROJECT, "Core/Src/stm32f4xx_it.c")

    assert "DMA_HandleTypeDef hdma_i2c1_rx;" in main
    assert "HAL_DMA_Init(&hdma_i2c1_rx)" in msp
    assert "__HAL_LINKDMA(i2cHandle, hdmarx, hdma_i2c1_rx);" in msp
    assert "__HAL_RCC_DMA1_CLK_ENABLE();" in msp
    assert "HAL_DMA_IRQHandler(&hdma_i2c1_rx);" in handlers
    assert result.warnings == []


# --------------------------------------------------------------------------
# Interrupts
# --------------------------------------------------------------------------


def test_an_interrupt_gets_a_vector_that_reaches_the_hal() -> None:
    with sandbox():
        scaffold_project(PROJECT, discovery_plan())
        handlers = generated(PROJECT, "Core/Src/stm32f4xx_it.c")
        header = generated(PROJECT, "Core/Inc/stm32f4xx_it.h")
        msp = generated(PROJECT, "Core/Src/stm32f4xx_hal_msp.c")

        assert "extern UART_HandleTypeDef huart2;" in handlers
        assert "void USART2_IRQHandler(void)" in handlers
        assert "HAL_UART_IRQHandler(&huart2);" in handlers
        assert "void USART2_IRQHandler(void);" in header
        assert "HAL_NVIC_EnableIRQ(USART2_IRQn);" in msp
        # HAL_Delay never returns without this one.
        assert "HAL_IncTick();" in handlers


def test_i2c_gets_both_of_its_vectors() -> None:
    plan = discovery_plan()
    plan.peripherals[1].nvic_priority = 6
    with sandbox():
        scaffold_project(PROJECT, plan)
        handlers = generated(PROJECT, "Core/Src/stm32f4xx_it.c")

        # An I2C driver wired to only the event vector hangs on the first
        # bus error, which looks exactly like a wiring fault.
        assert "HAL_I2C_EV_IRQHandler(&hi2c1);" in handlers
        assert "HAL_I2C_ER_IRQHandler(&hi2c1);" in handlers


def test_a_timer_that_shares_its_vector_is_reported_not_invented() -> None:
    plan = discovery_plan()
    plan.peripherals.append(PeripheralConfig(peripheral="TIM1", nvic_priority=3))
    with sandbox():
        result = scaffold_project(PROJECT, plan)
        handlers = generated(PROJECT, "Core/Src/stm32f4xx_it.c")

    # TIM1_IRQn does not exist on this part: the vector is TIM1_UP_TIM10_IRQn.
    assert "TIM1_IRQHandler" not in handlers
    assert any("shares a vector" in warning for warning in result.warnings)


def test_a_timer_with_nothing_to_drive_is_not_a_warning() -> None:
    plan = discovery_plan()
    plan.peripherals.append(PeripheralConfig(peripheral="TIM3", parameters={"Period": "9999"}))
    with sandbox():
        result = scaffold_project(PROJECT, plan)
        msp = generated(PROJECT, "Core/Src/stm32f4xx_hal_msp.c")

    # A time-base timer has no pins by design. Warning about it is how a
    # warning list stops being read.
    assert "TIM3" in result.configured
    assert "__HAL_RCC_TIM3_CLK_ENABLE();" in msp
    assert result.warnings == []


def test_a_peripheral_without_an_interrupt_gets_no_vector() -> None:
    with sandbox():
        scaffold_project(PROJECT, discovery_plan())
        handlers = generated(PROJECT, "Core/Src/stm32f4xx_it.c")

        assert "I2C1_EV_IRQHandler" not in handlers


# --------------------------------------------------------------------------
# The HAL configuration header
# --------------------------------------------------------------------------


def test_the_modules_compiled_are_exactly_the_modules_enabled() -> None:
    with sandbox():
        scaffold_project(PROJECT, discovery_plan())
        conf = generated(PROJECT, "Core/Inc/stm32f4xx_hal_conf.h")

        # Compiled, so switched on: otherwise the driver compiles to an empty
        # object and the link fails on HAL_I2C_Init with the source in plain
        # sight in the source list.
        assert "\n#define HAL_I2C_MODULE_ENABLED" in conf
        assert "\n#define HAL_UART_MODULE_ENABLED" in conf
        assert "\n#define HAL_MODULE_ENABLED" in conf
        # Not compiled, so switched off.
        assert "/* #define HAL_ADC_MODULE_ENABLED */" in conf
        assert "/* #define HAL_CRYP_MODULE_ENABLED */" in conf


def test_an_extension_driver_rides_on_its_parents_switch() -> None:
    assert halconf.macro_for("i2c_ex") == "HAL_I2C_MODULE_ENABLED"
    assert halconf.macro_for("flash_ramfunc") == "HAL_FLASH_MODULE_ENABLED"
    assert halconf.macro_for("cortex") == "HAL_CORTEX_MODULE_ENABLED"


def test_the_hal_is_told_which_crystal_is_on_the_board() -> None:
    with sandbox():
        result = scaffold_project(PROJECT, discovery_plan())
        conf = generated(PROJECT, "Core/Inc/stm32f4xx_hal_conf.h")

        # Left at ST's 25 MHz default, every HAL-computed baud rate and
        # timeout on an 8 MHz board is wrong by a factor of three, and the
        # board still boots -- which is why this is a test and not a comment.
        assert "((uint32_t)8000000U)" in conf
        assert "25000000" not in conf
        assert result.warnings == []


def test_the_crystal_is_found_in_both_spellings_st_has_shipped() -> None:
    older = "#define HAL_MODULE_ENABLED\n#define HSE_VALUE    ((uint32_t)25000000U)\n"
    newer = "#define HAL_MODULE_ENABLED\n#define HSE_VALUE    25000000U /*!< in Hz */\n"

    for template in (older, newer):
        text, warnings = halconf.configure(template, modules=[], hse_hz=8_000_000)
        assert "8000000" in text
        assert "25000000" not in text
        assert warnings == []


def test_a_crystal_this_header_does_not_mention_is_reported() -> None:
    text, warnings = halconf.configure(
        "#define HAL_MODULE_ENABLED\n", modules=[], hse_hz=8_000_000
    )

    # Failing quietly here is a board that runs at a third of its clock.
    assert "8000000" not in text
    assert any("HSE_VALUE" in warning for warning in warnings)


def test_a_header_that_is_not_st_s_template_is_refused() -> None:
    with pytest.raises(CodegenError) as error:
        halconf.configure("/* something else entirely */\n", modules=["gpio"])
    assert "HAL_MODULE_ENABLED" in str(error.value)


# --------------------------------------------------------------------------
# Regeneration
# --------------------------------------------------------------------------


def test_code_written_into_the_project_survives_regeneration() -> None:
    with sandbox():
        scaffold_project(PROJECT, discovery_plan())
        main = generated(PROJECT, "Core/Src/main.c")
        workspace.write_file(
            PROJECT,
            "Core/Src/main.c",
            main.replace(
                "/* USER CODE BEGIN WHILE */\n",
                "/* USER CODE BEGIN WHILE */\n    HAL_Delay(500);\n",
            ),
        )

        # The repair loop regenerates on every attempt. If that wiped the
        # application, it would be deleting the code it is trying to fix.
        plan = discovery_plan()
        plan.peripherals[0].parameters["BaudRate"] = "115200"
        scaffold_project(PROJECT, plan)
        rebuilt = generated(PROJECT, "Core/Src/main.c")

        assert "HAL_Delay(500);" in rebuilt
        assert "huart2.Init.BaudRate = 115200;" in rebuilt
        assert "huart2.Init.BaudRate = 9600;" not in rebuilt


def test_an_empty_user_region_does_not_overwrite_a_new_one() -> None:
    previous = "/* USER CODE BEGIN 1 */\n\n/* USER CODE END 1 */\n"
    fresh = "/* USER CODE BEGIN 1 */\nint generated = 1;\n/* USER CODE END 1 */\n"

    assert merge_user_code(previous, fresh) == fresh
    assert user_regions(fresh) == {"1": "\nint generated = 1;\n"}


def test_a_template_hole_is_an_error_not_an_empty_file() -> None:
    with pytest.raises(CodegenError) as error:
        render("gitignore.tmpl", {"NOT_THERE": "x"})
    assert "NOT_THERE" in str(error.value)

    with pytest.raises(CodegenError) as missing:
        render("main.h.tmpl", {})
    assert "PIN_DEFINES" in str(missing.value)
