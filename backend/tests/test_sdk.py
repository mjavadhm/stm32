"""Vendored ST drivers, tested without downloading anything (M4, P2).

The real SDK lives in the build image, so these tests build a miniature one
in a temp directory. What is worth asserting is not that shutil copies
files, but the decisions around it: which drivers a peripheral needs, which
files must *not* be copied, and whether a broken SDK produces an error a
human can act on.
"""

import contextlib
import stat
import tempfile
from pathlib import Path

import pytest

from app.build import workspace
from app.codegen import sdk
from app.core.config import settings

PROJECT = "proj1"

# Deliberately not the full list: flash_ramfunc is a core module that this
# fake release does not ship, and stm32f429xx.h is a device we did not ask
# for. Both are load-bearing in the tests below.
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


def write(path: Path, text: str = "/* vendor */\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_sdk(root: Path) -> None:
    """A miniature copy of what fetch-sdk.sh leaves in the image."""
    write(
        root / "VERSION",
        "stm32f4xx_hal_driver refs/tags/v1.8.3\ncmsis_device_f4 refs/tags/v2.6.10\n",
    )
    hal = root / sdk.HAL_DIR
    write(hal / "Inc/stm32f4xx_hal.h")
    write(hal / "Inc/stm32f4xx_hal_def.h")
    write(hal / "Inc/Legacy/stm32_hal_legacy.h")
    write(hal / "Src/stm32f4xx_hal.c")
    for module in FAKE_MODULES:
        write(hal / f"Src/stm32f4xx_hal_{module}.c")

    device = root / sdk.DEVICE_DIR
    write(device / "Include/stm32f4xx.h")
    write(device / "Include/system_stm32f4xx.h")
    write(device / "Include/stm32f407xx.h")
    write(device / "Include/stm32f429xx.h")
    write(device / "Source/Templates/system_stm32f4xx.c")
    write(device / "Source/Templates/gcc/startup_stm32f407xx.s")

    write(root / sdk.CMSIS_DIR / "Include/core_cm4.h")
    write(root / sdk.CMSIS_DIR / "Include/cmsis_gcc.h")


@contextlib.contextmanager
def sandbox(*, vendored: bool = True):
    """A temp workspace root and a temp SDK, restored afterwards."""
    previous = (settings.workspace_root, settings.cube_sdk_root)
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        settings.workspace_root = str(base / "workspaces")
        settings.cube_sdk_root = str(base / "sdk")
        if vendored:
            make_sdk(Path(settings.cube_sdk_root))
        try:
            yield base
        finally:
            settings.workspace_root, settings.cube_sdk_root = previous


def test_a_peripheral_name_loses_its_instance_number() -> None:
    assert sdk.family("SPI1") == "SPI"
    assert sdk.family("I2C1") == "I2C"
    assert sdk.family("usart2") == "USART"
    assert sdk.family(" TIM3 ") == "TIM"


def test_a_project_gets_the_drivers_its_peripherals_need() -> None:
    modules = sdk.modules_for(["SPI1", "SPI2", "USART2"])

    assert modules == sorted(set(modules))  # no duplicate compilation units
    assert "spi" in modules
    assert {"uart", "usart"} <= set(modules)
    assert {"rcc", "gpio", "cortex"} <= set(modules)  # always needed
    assert "i2c" not in modules


def test_a_peripheral_we_have_no_driver_for_is_reported_not_guessed() -> None:
    assert sdk.unsupported(["SPI1", "QUADSPI1"]) == ["QUADSPI"]


def test_only_the_selected_device_header_is_copied() -> None:
    with sandbox():
        sdk.copy_into(PROJECT, peripherals=["SPI1"])
        include = workspace.workspace_path(PROJECT) / sdk.DEVICE_DIR / "Include"

        assert (include / "stm32f407xx.h").is_file()
        # ~700 KB per part, fifty parts, one of them ever compiled.
        assert not (include / "stm32f429xx.h").exists()


def test_every_source_the_makefile_will_list_is_really_there() -> None:
    with sandbox():
        copied = sdk.copy_into(PROJECT, peripherals=["SPI1"])
        root = workspace.workspace_path(PROJECT)

        assert copied.sources == sorted(set(copied.sources), key=copied.sources.index)
        for relative in copied.sources:
            assert (root / relative).is_file(), relative
        assert "Core/Startup/startup_stm32f407xx.s" in copied.sources
        assert "Core/Src/system_stm32f4xx.c" in copied.sources
        assert f"{sdk.HAL_DIR}/Src/stm32f4xx_hal_spi.c" in copied.sources
        assert f"{sdk.HAL_DIR}/Src/stm32f4xx_hal_i2c.c" not in copied.sources


def test_a_module_missing_from_this_release_is_skipped_not_listed() -> None:
    with sandbox():
        copied = sdk.copy_into(PROJECT, peripherals=[])

        assert "flash_ramfunc" in copied.modules  # we asked for it
        assert not any("flash_ramfunc" in source for source in copied.sources)


def test_the_include_paths_exist_in_the_project() -> None:
    with sandbox():
        copied = sdk.copy_into(PROJECT, peripherals=["USART2"])
        root = workspace.workspace_path(PROJECT)

        for relative in copied.includes:
            assert (root / relative).is_dir(), relative


def test_the_project_stays_writable_for_the_build_container() -> None:
    with sandbox():
        sdk.copy_into(PROJECT, peripherals=["SPI1"])
        root = workspace.workspace_path(PROJECT)

        for relative in (".", sdk.HAL_DIR, f"{sdk.HAL_DIR}/Src", "Core/Startup"):
            mode = stat.S_IMODE((root / relative).stat().st_mode)
            assert mode == 0o777, relative


def test_missing_drivers_say_how_to_get_them() -> None:
    with sandbox(vendored=False):
        with pytest.raises(sdk.SdkError) as error:
            sdk.copy_into(PROJECT, peripherals=["SPI1"])

        assert "builder-image" in str(error.value)


def test_a_device_outside_the_vendored_family_is_refused() -> None:
    with sandbox():
        with pytest.raises(sdk.SdkError):
            sdk.copy_into(PROJECT, device="stm32h743zi")


def test_a_device_without_a_startup_file_is_refused() -> None:
    with sandbox():
        with pytest.raises(sdk.SdkError) as error:
            sdk.copy_into(PROJECT, device="stm32f411ce")

        assert "startup" in str(error.value)


def test_the_version_of_the_drivers_is_recorded_with_the_project() -> None:
    with sandbox():
        copied = sdk.copy_into(PROJECT, peripherals=["SPI1"])

        assert "v1.8.3" in copied.version
