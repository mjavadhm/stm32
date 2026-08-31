"""The MCU facts a project cannot be generated without.

Memory sizes, the FPU variant and the clock ceiling are datasheet facts, so
they live in a table rather than in a prompt. A model that is asked how much
RAM an STM32F407 has will usually be right, and "usually" produces a linker
script that silently overflows on the day it is wrong.

Adding a part is a row here. Everything downstream -- the linker script, the
compiler flags, the startup file, the HAL define -- follows from it.
"""

import re
from dataclasses import dataclass

from app.codegen.errors import CodegenError


@dataclass(frozen=True)
class Device:
    """One supported MCU."""

    part: str  # stm32f407xx: the CMSIS header and startup file name
    define: str  # STM32F407xx: what the HAL switches on
    cpu: str  # cortex-m4
    fpu: str  # fpv4-sp-d16
    float_abi: str  # hard
    flash_kb: int
    ram_kb: int
    ccm_kb: int  # 0 = no core-coupled memory
    max_hclk_hz: int
    # Bus ceilings. Not derivable from max_hclk_hz -- the F407 divides its
    # 168 MHz by 4 and 2, the F411 its 100 MHz by 2 and 1 -- and exceeding one
    # is a fault nothing reports: the peripheral simply misbehaves.
    apb1_max_hz: int = 0  # 0 = unknown, callers fall back to HCLK/4
    apb2_max_hz: int = 0

    @property
    def linker_name(self) -> str:
        return f"{self.define}_FLASH.ld"

    @property
    def flash_bytes(self) -> int:
        return self.flash_kb * 1024

    @property
    def ram_bytes(self) -> int:
        return self.ram_kb * 1024


DEVICES: dict[str, Device] = {
    "stm32f407xx": Device(
        part="stm32f407xx",
        define="STM32F407xx",
        cpu="cortex-m4",
        fpu="fpv4-sp-d16",
        float_abi="hard",
        flash_kb=1024,
        ram_kb=128,
        ccm_kb=64,
        max_hclk_hz=168_000_000,
        apb1_max_hz=42_000_000,
        apb2_max_hz=84_000_000,
    ),
    "stm32f411xe": Device(
        part="stm32f411xe",
        define="STM32F411xE",
        cpu="cortex-m4",
        fpu="fpv4-sp-d16",
        float_abi="hard",
        flash_kb=512,
        ram_kb=128,
        ccm_kb=0,
        max_hclk_hz=100_000_000,
        apb1_max_hz=50_000_000,
        apb2_max_hz=100_000_000,
    ),
}

_PART_RE = re.compile(r"stm32f4\d{2}")


def device_for(mcu: str) -> Device:
    """Resolve anything a plan might call an MCU into one table row.

    Plans arrive with "STM32F407VGT6", "stm32f407vg" or "STM32F407xx"
    depending on which datasheet the model was reading.
    """
    text = str(mcu or "").strip().lower().replace("-", "")
    match = _PART_RE.search(text)
    if match:
        for key, device in DEVICES.items():
            if key.startswith(match.group(0)):
                return device
    known = ", ".join(sorted(DEVICES))
    raise CodegenError(f"no device table for {mcu!r}; supported: {known}")
