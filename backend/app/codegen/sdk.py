"""Where a generated project gets its ST drivers from.

The HAL and CMSIS sources are not in this repository. They are downloaded
into the build image once (`deploy/builder/fetch-sdk.sh`, the only step in
this system that has internet) and mounted here read-only, so:

  * git never carries ~10 MB of third-party C that we do not edit
  * nothing is downloaded while a project is generated -- the builder has no
    network route at all
  * every project still gets its **own** copy of the drivers it uses, which
    is what makes a downloaded zip compile on a machine that has only a
    toolchain installed. STM32CubeMX copies them for the same reason.

This module is the single place that knows the vendored layout. When that
layout changes, `python -m scripts.sdk_check` fails here and says so, rather
than surfacing three steps later as a missing header inside a compile log.
"""

import re
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.build import workspace
from app.core.config import settings


class SdkError(RuntimeError):
    """The vendored drivers are missing, or do not look like we expect."""


HINT = (
    "run `make builder-image` (needs internet: it downloads ST's HAL/CMSIS), "
    "then `make sdk-refresh` if the image already existed"
)

# The layout produced by deploy/builder/fetch-sdk.sh. It mirrors CubeMX
# output so a generated project looks familiar to anyone who has used it.
HAL_DIR = "Drivers/STM32F4xx_HAL_Driver"
CMSIS_DIR = "Drivers/CMSIS"
DEVICE_DIR = f"{CMSIS_DIR}/Device/ST/STM32F4xx"

# What the generated Makefile passes to the compiler as -I, in CubeMX order.
INCLUDE_DIRS = (
    f"{HAL_DIR}/Inc",
    f"{HAL_DIR}/Inc/Legacy",
    f"{DEVICE_DIR}/Include",
    f"{CMSIS_DIR}/Include",
)

# Compiled into every project. Without these the HAL cannot start a clock,
# configure a pin or take an interrupt, whatever else the project does.
CORE_MODULES = (
    "cortex",
    "dma",
    "dma_ex",
    "exti",
    "flash",
    "flash_ex",
    "flash_ramfunc",
    "gpio",
    "pwr",
    "pwr_ex",
    "rcc",
    "rcc_ex",
)

# Peripheral family -> the HAL modules that implement it. Keys are what a
# CubeMXPlan calls a peripheral with the instance number removed, so SPI1,
# SPI2 and SPI3 all resolve to the same single driver.
PERIPHERAL_MODULES: dict[str, tuple[str, ...]] = {
    "ADC": ("adc", "adc_ex"),
    "CAN": ("can",),
    "CRC": ("crc",),
    "DAC": ("dac", "dac_ex"),
    "DCMI": ("dcmi", "dcmi_ex"),
    "ETH": ("eth",),
    "I2C": ("i2c", "i2c_ex"),
    "I2S": ("i2s", "i2s_ex"),
    "IWDG": ("iwdg",),
    "LTDC": ("ltdc", "ltdc_ex"),
    "RNG": ("rng",),
    "RTC": ("rtc", "rtc_ex"),
    "SAI": ("sai", "sai_ex"),
    "SDIO": ("sd",),
    "SPI": ("spi",),
    "TIM": ("tim", "tim_ex"),
    "UART": ("uart",),
    "USART": ("uart", "usart"),
    "WWDG": ("wwdg",),
}

_TRAILING_DIGITS = re.compile(r"\d+$")
# Only the F4 family is vendored today. A device outside it must fail here,
# where the message can say so, and not as a missing startup file.
_DEVICE_RE = re.compile(r"^stm32f4\d{2}[a-z]{2}$")


def family(peripheral: str) -> str:
    """SPI1 -> SPI, I2C1 -> I2C, USART2 -> USART."""
    return _TRAILING_DIGITS.sub("", str(peripheral or "").strip().upper())


def modules_for(peripherals: Iterable[str]) -> list[str]:
    """The HAL modules a project with these peripherals has to compile."""
    modules = set(CORE_MODULES)
    for peripheral in peripherals:
        modules.update(PERIPHERAL_MODULES.get(family(peripheral), ()))
    return sorted(modules)


def unsupported(peripherals: Iterable[str]) -> list[str]:
    """Peripherals with no HAL module here. The caller decides how loud."""
    names = {family(peripheral) for peripheral in peripherals}
    return sorted(name for name in names if name and name not in PERIPHERAL_MODULES)


def sdk_root() -> Path:
    """Read the setting on every call so tests can point it at a temp dir."""
    return Path(settings.cube_sdk_root)


def sdk_version() -> str:
    """The refs the image actually downloaded, as recorded by fetch-sdk.sh."""
    try:
        return " | ".join((sdk_root() / "VERSION").read_text("utf-8").split())
    except OSError:
        return ""


def require_sdk() -> Path:
    """The SDK root, or an error that says how to get one."""
    root = sdk_root()
    for name in (
        f"{HAL_DIR}/Src/stm32f4xx_hal.c",
        f"{HAL_DIR}/Inc/stm32f4xx_hal.h",
        f"{DEVICE_DIR}/Include/stm32f4xx.h",
        f"{CMSIS_DIR}/Include/core_cm4.h",
    ):
        if not (root / name).is_file():
            raise SdkError(f"no ST drivers under {root} ({name} is missing) -- {HINT}")
    return root


@dataclass
class SdkCopy:
    """What landed in the project, in the shape the Makefile needs."""

    version: str = ""
    modules: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    headers: int = 0
    unsupported: list[str] = field(default_factory=list)


def _copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def _copy_headers(source_dir: Path, target_dir: Path) -> int:
    copied = 0
    for path in sorted(source_dir.rglob("*.h")):
        _copy(path, target_dir / path.relative_to(source_dir))
        copied += 1
    return copied


def copy_into(
    project_id: str,
    *,
    peripherals: Sequence[str] = (),
    device: str = "stm32f407xx",
) -> SdkCopy:
    """Copy the drivers this project needs into the project's workspace."""
    root = require_sdk()
    device = str(device or "").strip().lower()
    if not _DEVICE_RE.match(device):
        raise SdkError(f"unsupported device {device!r}; only STM32F4 is vendored")

    startup = root / DEVICE_DIR / "Source/Templates/gcc" / f"startup_{device}.s"
    if not startup.is_file():
        raise SdkError(f"no startup file for {device} ({startup.name}) -- {HINT}")

    modules = modules_for(peripherals)
    workspace.ensure_workspace(project_id)
    target = workspace.workspace_path(project_id)

    headers = _copy_headers(root / HAL_DIR / "Inc", target / HAL_DIR / "Inc")
    headers += _copy_headers(root / CMSIS_DIR / "Include", target / CMSIS_DIR / "Include")

    # The device Include directory holds one ~700 KB header per F4 part, and
    # exactly one of them is ever compiled: stm32f4xx.h picks it from the
    # -DSTM32F407xx define. Copying all fifty would add ~35 MB to every
    # download to satisfy an #include that never happens.
    for name in ("stm32f4xx.h", "system_stm32f4xx.h", f"{device}.h"):
        source = root / DEVICE_DIR / "Include" / name
        if not source.is_file():
            raise SdkError(f"missing device header {name} -- {HINT}")
        _copy(source, target / DEVICE_DIR / "Include" / name)
        headers += 1

    sources: list[str] = []
    for name in ["stm32f4xx_hal.c", *(f"stm32f4xx_hal_{module}.c" for module in modules)]:
        source = root / HAL_DIR / "Src" / name
        # Releases differ in which _ex modules exist. A driver we cannot find
        # is a driver we do not compile; the alternative is a Makefile that
        # lists a file that is not there.
        if source.is_file():
            _copy(source, target / HAL_DIR / "Src" / name)
            sources.append(f"{HAL_DIR}/Src/{name}")

    _copy(
        root / DEVICE_DIR / "Source/Templates/system_stm32f4xx.c",
        target / "Core/Src/system_stm32f4xx.c",
    )
    sources.append("Core/Src/system_stm32f4xx.c")
    _copy(startup, target / "Core/Startup" / startup.name)
    sources.append(f"Core/Startup/{startup.name}")

    # Directories created here are new, and the build container writes into
    # the project as a different user (see workspace.relax_permissions).
    workspace.relax_permissions(project_id)

    return SdkCopy(
        version=sdk_version(),
        modules=modules,
        sources=sources,
        includes=list(INCLUDE_DIRS),
        headers=headers,
        unsupported=unsupported(peripherals),
    )
