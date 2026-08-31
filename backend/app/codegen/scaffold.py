"""A plan in, a project that compiles out.

The firmware agent never sees an empty directory. By the time it is asked for
application code, the workspace already holds ST's drivers, a linker script
with the right memory map, a startup file, interrupt vectors, the clock tree
and every peripheral the plan asked for -- all of it deterministic, none of it
written by a model.

That split is the whole point. The scaffolding is where a model's mistakes are
silent (a wrong wait state, a missing clock enable, a linker symbol renamed),
and the application is where its mistakes are loud. Generating the first and
asking for the second is what makes "it compiled" mean something.
"""

import re
from dataclasses import dataclass, field

from app.build import workspace
from app.codegen import halconf, peripherals, sdk
from app.codegen.devices import device_for
from app.codegen.errors import CodegenError
from app.codegen.render import merge_user_code, render
from app.orchestrator.contracts import CubeMXPlan

# Files with USER CODE regions: regenerating one keeps whatever was written
# into it. Everything else is overwritten without ceremony.
PRESERVED = (
    "Core/Inc/main.h",
    "Core/Inc/stm32f4xx_it.h",
    "Core/Src/main.c",
    "Core/Src/stm32f4xx_hal_msp.c",
    "Core/Src/stm32f4xx_it.c",
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class Scaffold:
    """What was generated, in the shape a report or a build needs."""

    project_id: str
    device: str = ""
    target: str = ""
    files: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    sdk_version: str = ""
    configured: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def slug(text: str, fallback: str) -> str:
    cleaned = _SLUG_RE.sub("-", str(text or "").strip().lower()).strip("-")
    return cleaned or fallback


def _mhz(hertz: int) -> str:
    if hertz <= 0:
        return "?"
    value = hertz / 1_000_000
    return f"{value:.6g} MHz"


def _make_list(paths: list[str]) -> str:
    return " \\\n".join(paths)


def _clock_summary(plan: CubeMXPlan) -> str:
    clock = plan.clock
    source = str(clock.source or "hsi").upper().replace("_BYPASS", " (bypass)")
    if clock.source and str(clock.source).lower().startswith("hse") and clock.hse_hz:
        source = f"{source} {_mhz(clock.hse_hz)}"
    parts = [f"- Source: {source}"]
    if clock.pll_n:
        parts.append(
            f"- PLL: M={clock.pll_m}, N={clock.pll_n}, P={clock.pll_p}, Q={clock.pll_q}"
        )
    parts.append(f"- SYSCLK: {_mhz(clock.sysclk_hz)}, HCLK: {_mhz(clock.hclk_hz)}")
    parts.append(f"- APB1: {_mhz(clock.apb1_hz)}, APB2: {_mhz(clock.apb2_hz)}")
    parts.append(f"- Flash wait states: {peripherals.flash_latency(clock.hclk_hz)}")
    if clock.citation:
        parts.append(f"- Source of these numbers: {clock.citation}")
    return "\n".join(parts)


def _peripheral_summary(plan: CubeMXPlan, configured: list[str]) -> str:
    if not plan.peripherals:
        return "No peripherals in the plan: GPIO and the system clock only."
    rows = ["| Peripheral | Handle | Pins | Generated |", "| --- | --- | --- | --- |"]
    for config in plan.peripherals:
        name = str(config.peripheral or "").strip().upper()
        pins = ", ".join(
            f"{assignment.pin} ({assignment.signal})" if assignment.signal else assignment.pin
            for assignment in plan.pins
            if str(assignment.peripheral or "").strip().upper() == name
        )
        generated = "yes" if name in configured else "no -- write it by hand"
        rows.append(f"| {name} | `{plan.handle(name)}` | {pins or '--'} | {generated} |")
    return "\n".join(rows)


def _previous(project_id: str) -> dict[str, str]:
    """Read the USER CODE carriers before anything is deleted."""
    kept: dict[str, str] = {}
    if not workspace.exists(project_id):
        return kept
    for relative in PRESERVED:
        path = workspace.safe_join(project_id, relative)
        if path.is_file():
            kept[relative] = path.read_text(encoding="utf-8", errors="replace")
    return kept


def _hal_conf(modules: list[str], plan: CubeMXPlan) -> tuple[str, list[str]]:
    template = sdk.sdk_root() / sdk.HAL_DIR / "Inc" / halconf.TEMPLATE_NAME
    if not template.is_file():
        raise CodegenError(f"{halconf.TEMPLATE_NAME} is not in the drivers -- {sdk.HINT}")
    hse = plan.clock.hse_hz if str(plan.clock.source or "").lower().startswith("hse") else 0
    return halconf.configure(
        template.read_text(encoding="utf-8", errors="replace"),
        modules=modules,
        hse_hz=hse,
    )


def scaffold_project(
    project_id: str,
    plan: CubeMXPlan,
    *,
    clean: bool = True,
    target: str = "",
    summary: str = "",
) -> Scaffold:
    """Generate the whole project skeleton into the project's workspace."""
    device = device_for(plan.mcu)
    name = slug(target or plan.board or plan.mcu, device.part)
    result = Scaffold(project_id=project_id, device=device.part, target=name)

    if not plan.validated:
        result.warnings.append(
            "the plan was never checked against the MCU's pin table, so the pin "
            "assignments below are the model's word alone"
        )

    kept = _previous(project_id)
    if clean:
        workspace.ensure_workspace(project_id, clean=True)

    copy = sdk.copy_into(
        project_id,
        peripherals=[config.peripheral for config in plan.peripherals],
        device=device.part,
    )
    result.sdk_version = copy.version
    for unsupported in copy.unsupported:
        result.warnings.append(
            f"{unsupported}: no HAL driver for it in this release, so nothing was "
            "copied for it"
        )

    fragments = peripherals.generate(plan, device)
    result.warnings.extend(fragments.warnings)
    result.configured = fragments.configured

    conf_text, conf_warnings = _hal_conf(copy.modules, plan)
    result.warnings.extend(conf_warnings)

    c_sources = [
        "Core/Src/main.c",
        "Core/Src/stm32f4xx_it.c",
        "Core/Src/stm32f4xx_hal_msp.c",
        *[path for path in copy.sources if path.endswith(".c")],
    ]
    asm_sources = [path for path in copy.sources if path.endswith(".s")]
    includes = ["Core/Inc", *copy.includes]
    result.sources = [*c_sources, *asm_sources]
    result.includes = includes

    ccm = ""
    if device.ccm_kb:
        ccm = (
            f"  CCMRAM (xrw) : ORIGIN = 0x10000000, LENGTH = {device.ccm_kb}K\n"
        )
    linker_name = device.linker_name

    rendered = {
        "Makefile": render(
            "makefile.tmpl",
            {
                "TARGET": name,
                "CPU": device.cpu,
                "FPU": device.fpu,
                "FLOAT_ABI": device.float_abi,
                "DEFINE": device.define,
                "C_INCLUDES": _make_list([f"-I{path}" for path in includes]),
                "C_SOURCES": _make_list(c_sources),
                "ASM_SOURCES": _make_list(asm_sources),
                "LDSCRIPT": linker_name,
            },
        ),
        linker_name: render(
            "linker.ld.tmpl",
            {
                "DEFINE": device.define,
                "RAM_KB": str(device.ram_kb),
                "FLASH_KB": str(device.flash_kb),
                "CCM_MEMORY": ccm,
            },
        ),
        "Core/Inc/main.h": render("main.h.tmpl", {"PIN_DEFINES": fragments.pin_defines}),
        "Core/Inc/stm32f4xx_it.h": render(
            "it.h.tmpl", {"IRQ_PROTOTYPES": fragments.irq_prototypes}
        ),
        "Core/Inc/stm32f4xx_hal_conf.h": conf_text,
        "Core/Src/main.c": render(
            "main.c.tmpl",
            {
                "HANDLES": fragments.handles,
                "PROTOTYPES": fragments.prototypes,
                "INIT_CALLS": fragments.init_calls,
                "CLOCK_CONFIG": fragments.clock_config,
                "GPIO_INIT": fragments.gpio_init,
                "INIT_FUNCTIONS": fragments.init_functions,
            },
        ),
        "Core/Src/stm32f4xx_it.c": render(
            "it.c.tmpl",
            {"EXTERNS": fragments.externs, "IRQ_HANDLERS": fragments.irq_handlers},
        ),
        "Core/Src/stm32f4xx_hal_msp.c": render(
            "msp.c.tmpl",
            {
                "MSP_EXTERNS": fragments.msp_externs,
                "MSP_FUNCTIONS": fragments.msp_functions,
            },
        ),
        ".gitignore": render("gitignore.tmpl", {}),
        "README.md": render(
            "readme.md.tmpl",
            {
                "TARGET": name,
                "MCU": plan.mcu or device.define,
                "SUMMARY": summary or "Firmware project generated from a design plan.",
                "LDSCRIPT": linker_name,
                "CLOCK_SUMMARY": _clock_summary(plan),
                "PERIPHERAL_SUMMARY": _peripheral_summary(plan, fragments.configured),
            },
        ),
    }

    for relative, contents in rendered.items():
        previous = kept.get(relative)
        if previous is not None:
            contents = merge_user_code(previous, contents)
        workspace.write_file(project_id, relative, contents)
        result.files.append(relative)

    # The Makefile is generated from this list, so a name in it that is not on
    # disk is our bug, and it should be reported here rather than as a compile
    # error inside a container two steps later.
    missing = [
        path for path in result.sources if not workspace.safe_join(project_id, path).is_file()
    ]
    if missing:
        raise CodegenError(f"generated a Makefile listing files that do not exist: {missing}")

    workspace.relax_permissions(project_id)
    result.files.sort()
    return result
