"""stm32f4xx_hal_conf.h, derived from the driver list instead of guessed.

This header decides which parts of the HAL exist at all. Every driver source
is wrapped in `#ifdef HAL_<X>_MODULE_ENABLED`, so a header that disagrees with
the Makefile produces the most confusing failure in the whole toolchain: the
file compiles to an empty object, and the link fails with
`undefined reference to HAL_SPI_Init` while the source sits right there in the
source list.

Rather than carry a 400-line copy of this header per driver release, we patch
ST's own `stm32f4xx_hal_conf_template.h` -- the file the vendor ships for
exactly this purpose. It always matches the release we downloaded.
"""

import re

from app.codegen.errors import CodegenError

TEMPLATE_NAME = "stm32f4xx_hal_conf_template.h"
MASTER_MACRO = "HAL_MODULE_ENABLED"

_MACRO_RE = re.compile(r"HAL_[A-Z0-9]+(?:_[A-Z0-9]+)*_MODULE_ENABLED")

# ST has spelled this two ways across releases -- `((uint32_t)25000000U)` in
# the older headers and a plain `25000000U` in the newer ones -- so the cast
# has to be optional. A regex that only knew the old spelling matched nothing
# in v1.8.3 and left the board running on a 25 MHz crystal it does not have.
_HSE_RE = re.compile(
    r"(#[ \t]*define[ \t]+HSE_VALUE[ \t]+)"
    r"\(*[ \t]*(?:\([ \t]*uint32_t[ \t]*\)[ \t]*)?\d+[uU]?[lL]*[ \t]*\)*"
)


def macro_for(module: str) -> str:
    """A HAL source module name -> the macro that switches it on.

    The extension modules share their parent's switch: stm32f4xx_hal_i2c_ex.c
    is compiled under HAL_I2C_MODULE_ENABLED, not a macro of its own.
    """
    base = str(module or "").strip().lower()
    base = base.removesuffix("_ex").removesuffix("_ramfunc")
    return f"HAL_{base.upper()}_MODULE_ENABLED"


def configure(template: str, *, modules: list[str], hse_hz: int = 0) -> tuple[str, list[str]]:
    """Enable exactly the modules we compile, and nothing else.

    Returns the header and any warnings. Leaving a module enabled that we do
    not compile is not harmless: the HAL's own headers then declare handles
    and callbacks for a driver that will not be linked.
    """
    warnings: list[str] = []
    if MASTER_MACRO not in template:
        raise CodegenError(
            f"{TEMPLATE_NAME} does not look like ST's HAL configuration template "
            f"({MASTER_MACRO} is missing)"
        )

    wanted = {MASTER_MACRO} | {macro_for(module) for module in modules}
    present = set(_MACRO_RE.findall(template)) | {MASTER_MACRO}

    for macro in sorted(wanted - present):
        warnings.append(
            f"{macro} is not offered by this HAL release; the driver is compiled "
            "but its header switch is missing"
        )

    text = template
    for macro in sorted(present):
        line = re.compile(
            rf"^[ \t]*(?:/\*[ \t]*)?#[ \t]*define[ \t]+{macro}\b.*$",
            re.MULTILINE,
        )
        replacement = f"#define {macro}" if macro in wanted else f"/* #define {macro} */"
        text, count = line.subn(replacement, text)
        if not count:
            warnings.append(f"{macro} is referenced by the template but never defined")

    if hse_hz > 0:
        text, count = _HSE_RE.subn(rf"\g<1>((uint32_t){hse_hz}U)", text, count=1)
        if not count:
            warnings.append(
                f"could not set HSE_VALUE to {hse_hz} Hz in {TEMPLATE_NAME}; "
                "the HAL will compute baud rates from the default crystal"
            )

    header = (
        "/* Generated from ST's stm32f4xx_hal_conf_template.h: the modules\n"
        " * enabled below are exactly the driver sources in the Makefile. */\n"
    )
    return header + text, warnings
