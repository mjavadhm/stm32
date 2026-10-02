"""C_SOURCES must list every file once (a duplicate links it twice)."""

from app.codegen.scaffold import MANAGED_SOURCES, c_source_list


def test_refresh_glob_does_not_duplicate_sdk_system_file():
    sdk = [
        "Core/Src/system_stm32f4xx.c",
        "Drivers/STM32F4xx_HAL_Driver/Src/stm32f4xx_hal.c",
        "startup_stm32f407xx.s",
    ]
    globbed = [
        "Core/Src/main.c",
        "Core/Src/mpu6050.c",
        "Core/Src/stm32f4xx_hal_msp.c",
        "Core/Src/stm32f4xx_it.c",
        "Core/Src/system_stm32f4xx.c",
    ]
    sources = c_source_list(globbed, sdk)
    assert len(sources) == len(set(sources))
    assert sources.count("Core/Src/system_stm32f4xx.c") == 1
    assert sources[: len(MANAGED_SOURCES)] == list(MANAGED_SOURCES)
    assert "Core/Src/mpu6050.c" in sources
    assert "startup_stm32f407xx.s" not in sources


def test_extra_sources_repeated_are_listed_once():
    sources = c_source_list(["Core/Src/a.c", "Core/Src/a.c", "Core/Inc/a.h"], [])
    assert sources == [*MANAGED_SOURCES, "Core/Src/a.c"]
