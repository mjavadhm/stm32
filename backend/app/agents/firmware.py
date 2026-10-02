"""P4 Firmware agent: step-by-step code generation following implementation_order.

The firmware agent takes the architectural design (Architecture), validated
hardware configuration (CubeMXPlan), and hardware evidence (HardwareFindings)
to generate complete, buildable C/H application and driver source files.

Key rules:
- Step-by-step generation along Architecture.implementation_order.
- Each file is generated with a focused prompt containing only relevant evidence
  and exported symbols from earlier steps (token budget conservation).
- Scaffold files (main.c, main.h) strictly enforce USER CODE regions to prevent
  overwriting clock and peripheral init routines.
- Concrete CubeMX peripheral handles (e.g. hspi1, huart2) are provided explicitly.
"""

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from app.agents.base import request_contract
from app.agents.repair import repair_firmware
from app.build import workspace
from app.codegen.peripherals import HANDLE_TYPES
from app.codegen.render import merge_user_code, render, user_regions
from app.codegen.scaffold import scaffold_project
from app.codegen.sdk import family
from app.core.llm import get_agent_llm, is_agent_enabled
from app.orchestrator.contracts import (
    Architecture,
    ContractError,
    BuildResult,
    CubeMXPlan,
    FirmwareBundle,
    HardwareFindings,
    ImplementationStep,
    Module,
    Requirements,
    SourceFile,
    dump,
    parse_stored,
)

logger = logging.getLogger(__name__)
AGENT_NAME = "firmware"

# Below this HardwareFindings.coverage the bundle is flagged "unverified" rather
# than withheld: code without a documented basis must be detectable, not banned
# (docs/m4-plan.md, P4).
_LOW_COVERAGE = 0.5

# HAL calls the deterministic scaffold and MSP layer always emit: clock, GPIO,
# NVIC, power and flash plumbing, plus the handful of core entry points. The
# application may lean on these without a datasheet citation, so they never
# count as an undocumented assumption (P4, docs/m4-plan.md:160-181).
_ALWAYS_AVAILABLE_APIS = frozenset(
    {
        "HAL_Init",
        "HAL_DeInit",
        "HAL_Delay",
        "HAL_GetTick",
        "HAL_IncTick",
        "HAL_GetUID",
        "HAL_NVIC_SystemReset",
    }
)
_ALWAYS_AVAILABLE_PREFIXES = (
    "HAL_GPIO_",
    "HAL_RCC_",
    "__HAL_RCC_",
    "HAL_NVIC_",
    "HAL_PWR",
    "__HAL_PWR",
    "__HAL_FLASH",
    "HAL_FLASH_",
    "HAL_SYSTICK_",
)
# Matches HAL functions (`HAL_SPI_Transmit`) and the double-underscore HAL
# macros (`__HAL_RCC_...`) alike -- 0, 1 or 2 leading underscores. Plain HAL_
# calls are the whole point of the check, so the underscores must be optional.
_HAL_CALL_RE = re.compile(r"\b_{0,2}HAL_[A-Za-z0-9_]+")
# How many undocumented APIs to name before the note just says "and N more".
_MAX_LISTED_APIS = 8

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_DEFINE_RE = re.compile(r"#\s*define\s+(?P<name>\w+(?:\([^)]*\))?)\s+(?P<body>\S.*)$")

_SYSTEM_PROMPT = """You are an expert embedded firmware engineer writing C for STM32.

Write complete, compile-clean C source or header files according to the requirements,
architecture, and peripheral configuration.

Rules:
1. Always use exact CubeMX HAL handle names provided (e.g. `hspi1`, `huart2`, `hi2c1`).
   Do NOT invent handle names like `spi1_handle`.
2. For `Core/Src/main.c` and `Core/Inc/main.h`:
   - All user code MUST be placed inside the standard CubeMX user code markers:
     `/* USER CODE BEGIN <Section> */`
     ... your code ...
     `/* USER CODE END <Section> */`
   - Common sections for main.c: `Includes`, `PV` (variables), `PFP` (prototypes),
     `2` (initialization before while loop), `WHILE` (superloop body), `4` (functions).
   - Do NOT rewrite SystemClock_Config or MX_*_Init functions; place your setup in
     USER CODE BEGIN 2 and loop logic in USER CODE BEGIN WHILE.
3. For custom driver or module files (e.g. `Core/Inc/sensor.h`, `Core/Src/sensor.c`):
   - Include standard guards (`#ifndef ... #define ... #endif`).
   - `#include "main.h"` to access HAL definitions and peripheral handles.
   - Write complete, robust production-ready code with error checking.
4. Only cite references from allowed citations for this step. If an API usage
   is not covered by retrieved documentation, leave citations empty and note it
   under "assumptions".
5. Reply with ONLY a JSON object in this format:
{
  "path": "Core/Src/example.c",
  "purpose": "Brief description of the file",
  "contents": "/* Complete file contents or USER CODE sections */",
  "citations": [],
  "assumptions": []
}"""


class FileProposal(BaseModel):
    path: str
    purpose: str = ""
    contents: str = ""
    citations: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


def _cubemx_context(plan: CubeMXPlan) -> str:
    clk = plan.clock
    lines = [
        f"MCU: {plan.mcu or 'STM32F407VGTx'}",
        f"Board: {plan.board or 'Custom'}",
        (
            f"Clock: SYSCLK={clk.sysclk_hz} Hz, HCLK={clk.hclk_hz} Hz, "
            f"APB1={clk.apb1_hz} Hz, APB2={clk.apb2_hz} Hz"
        ),
    ]
    if plan.peripherals:
        lines.append("\nConfigured Peripherals & Handles:")
        for p in plan.peripherals:
            h = plan.handle(p.peripheral)
            lines.append(f"- Peripheral: {p.peripheral}, Handle: `{h}`, Mode: {p.mode}")
            for d in p.dma:
                req = d.request or p.peripheral
                lines.append(
                    f"  * DMA: request={req}, stream={d.stream}, "
                    f"channel={d.channel}, dir={d.direction}"
                )
    if plan.pins:
        lines.append("\nAssigned Pins:")
        for pin in plan.pins:
            sig = pin.signal or pin.peripheral
            lines.append(f"- {pin.pin}: {sig} (mode={pin.mode}, pull={pin.pull})")
    return "\n".join(lines)


def _step_evidence(step: ImplementationStep, hardware: HardwareFindings) -> str:
    allowed = set(step.citations)
    lines = []
    for finding in hardware.findings:
        topic_citations = [c for c in finding.citations if c in allowed]
        if topic_citations or (finding.topic and finding.topic.lower() in step.title.lower()):
            lines.append(f"- {finding.topic}: {finding.answer}")
            if finding.citations:
                lines.append(f"  Allowed citations: {', '.join(finding.citations)}")
    return "\n".join(lines) if lines else "None available for this step."


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _next_significant(code: str, start: int) -> str:
    """The first `;` or `{` at or after `start`, or "" if neither appears.

    Used to tell a closed struct/typedef body (`} Name;` -- more to come before
    the `;`) from a function body with no trailing `;` (the next token opens a
    new block, or the input ends).
    """
    for ch in code[start:]:
        if ch == ";":
            return ";"
        if ch == "{":
            return "{"
    return ""


def _split_declarations(code: str) -> list[str]:
    """Top-level, brace-aware `;`-terminated statements from C source.

    Prototypes and typedef/struct/enum bodies come back whole; a stray function
    *definition* is reduced to its signature so a header's inline body is not
    dragged into the next step's prompt.
    """
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    i = 0
    n = len(code)
    while i < n:
        ch = code[i]
        buf.append(ch)
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
            if depth == 0 and _next_significant(code, i + 1) != ";":
                head = _collapse_ws("".join(buf).split("{", 1)[0])
                if head:
                    out.append(f"{head};")
                buf = []
        elif ch == ";" and depth == 0:
            statement = _collapse_ws("".join(buf))
            if statement and statement != ";":
                out.append(statement)
            buf = []
        i += 1
    tail = _collapse_ws("".join(buf))
    if tail and tail != ";":
        out.append(tail if tail.endswith(";") else f"{tail};")
    return out


def _header_signatures(contents: str) -> str:
    """Reduce a header to its interface: value macros and declarations.

    The P4 token budget wants later steps to see *what a header offers*, not
    every line of it. Comments, include guards and `#include` lines carry no
    interface, so they are dropped; object/function-like macros with a value
    and every top-level declaration are kept. Falls back to the stripped body
    if nothing recognisable is found, so an unusual header is never blanked.
    """
    text = _BLOCK_COMMENT_RE.sub("", contents)
    text = _LINE_COMMENT_RE.sub("", text)

    macros: list[str] = []
    code_lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            match = _DEFINE_RE.match(stripped)
            if match:
                macros.append(f"#define {match.group('name')} {match.group('body').strip()}")
            # Include guards, #include and #if/#endif carry no interface.
            continue
        code_lines.append(raw)

    kept = macros + _split_declarations("\n".join(code_lines))
    if kept:
        return "\n".join(kept)
    collapsed = _collapse_ws(text)
    return collapsed or contents.strip()


def _undocumented_apis(contents: str, support: str) -> list[str]:
    """HAL calls in `contents` with no basis in `support` or the scaffold.

    P4 rule: an API the retrieved evidence does not cover is an assumption, not
    a citation. Clock/GPIO/NVIC plumbing the deterministic layer always emits is
    excluded, so the caller's note names only application-level calls that lack
    a documented basis.
    """
    missing: list[str] = []
    seen: set[str] = set()
    for symbol in _HAL_CALL_RE.findall(contents):
        if symbol in seen:
            continue
        seen.add(symbol)
        if symbol in _ALWAYS_AVAILABLE_APIS:
            continue
        if symbol.startswith(_ALWAYS_AVAILABLE_PREFIXES):
            continue
        if symbol in support:
            continue
        missing.append(symbol)
    return missing


def _headers_context(generated_files: list[SourceFile]) -> str:
    """Signatures of previously generated headers, so later steps know the
    prototypes and types available without spending the whole header on it."""
    headers = [f for f in generated_files if f.path.endswith(".h")]
    if not headers:
        return "No custom headers created yet."
    parts = []
    for h in headers:
        parts.append(f"=== {h.path} ===\n{_header_signatures(h.contents)}\n")
    return "\n".join(parts)


def _matching_module_context(step: ImplementationStep, modules: list[Module]) -> str:
    matching = [m for m in modules if m.name in step.modules or m.path in step.files]
    if not matching:
        return f"Step: {step.title} - {step.detail}"
    parts = [f"Step: {step.title} - {step.detail}"]
    for m in matching:
        parts.append(
            f"- Module `{m.name}` ({m.path}): responsibility: {m.responsibility}, "
            f"layer: {m.layer}, depends_on: {', '.join(m.depends_on) or 'none'}"
        )
    return "\n".join(parts)


def _scaffold_fallback(relative_path: str, plan: CubeMXPlan) -> str:
    """Return a minimal scaffold template if not already present on disk."""
    if relative_path == "Core/Src/main.c":
        # Definitions, spelled the way the real scaffold spells them: the HAL
        # type comes from the peripheral family (SPI1 -> SPI_HandleTypeDef,
        # USART2 -> UART_HandleTypeDef), never SPI1_HandleTypeDef.
        handles = "\n".join(
            f"{HANDLE_TYPES[family(p.peripheral)]} {plan.handle(p.peripheral)};"
            for p in plan.peripherals
            if family(p.peripheral) in HANDLE_TYPES
        )
        if handles:
            handles += "\n"
        return render(
            "main.c.tmpl",
            {
                "HANDLES": handles,
                "PROTOTYPES": "",
                "INIT_CALLS": "",
                "CLOCK_CONFIG": "void SystemClock_Config(void) {}\n",
                "GPIO_INIT": "static void MX_GPIO_Init(void) {}\n",
                "INIT_FUNCTIONS": "",
            },
        )
    if relative_path == "Core/Inc/main.h":
        return render("main.h.tmpl", {"PIN_DEFINES": ""})
    return ""


def build_file_prompt(
    file_path: str,
    step: ImplementationStep,
    requirements: Requirements,
    hardware: HardwareFindings,
    architecture: Architecture,
    plan: CubeMXPlan,
    generated_files: list[SourceFile],
    scaffold_content: str = "",
) -> str:
    sections = [
        f"# Target File: `{file_path}`",
        f"Generate complete implementation for `{file_path}`.",
        "\n# Project Overview",
        f"Summary: {requirements.summary or 'STM32 firmware project'}",
        f"Driver layer: {architecture.driver_layer}",
        "\n# Hardware & Peripheral Configuration",
        _cubemx_context(plan),
        "\n# Step Specification",
        _matching_module_context(step, architecture.modules),
        "\n# Relevant Hardware Evidence",
        _step_evidence(step, hardware),
        "\n# Previously Generated Headers & Declarations",
        _headers_context(generated_files),
    ]

    if file_path in ("Core/Src/main.c", "Core/Inc/main.h") and scaffold_content:
        sections.extend([
            f"\n# Existing Scaffold for `{file_path}`",
            "Managed scaffold file. You MUST provide code inside USER CODE markers.",
            "```c",
            scaffold_content,
            "```",
        ])

    return "\n".join(sections)


def _merge_scaffold_file(file_path: str, proposed_code: str, base_scaffold: str) -> str:
    """Merge user code into scaffold file ensuring system init is preserved."""
    if not base_scaffold:
        return proposed_code

    regions = user_regions(proposed_code)
    if regions:
        return merge_user_code(proposed_code, base_scaffold)

    # Fallback if model omitted markers: place code into USER CODE BEGIN 2
    logger.warning(
        "No USER CODE markers found in proposal for %s; inserting into USER CODE BEGIN 2",
        file_path,
    )
    if file_path == "Core/Src/main.c":
        wrapped = f"/* USER CODE BEGIN 2 */\n{proposed_code}\n/* USER CODE END 2 */"
        return merge_user_code(wrapped, base_scaffold)
    return proposed_code


async def generate_firmware(
    requirements: Requirements,
    hardware: HardwareFindings,
    architecture: Architecture,
    plan: CubeMXPlan,
    *,
    project_id: str = "",
    project_name: str = "",
    llm: Any = None,
) -> tuple[FirmwareBundle, list[str]]:
    """Generate all firmware source and header files step-by-step."""
    if llm is None:
        if not is_agent_enabled(AGENT_NAME):
            warning = "Firmware agent is disabled; no source generated."
            return FirmwareBundle(warnings=[warning]), [warning]
        llm = get_agent_llm(AGENT_NAME)

    warnings: list[str] = []
    assumptions: list[str] = list(architecture.assumptions)
    citations: list[str] = []
    generated_files: list[SourceFile] = []
    known_citations = set(hardware.citations)

    # Corpus the undocumented-API check tests each file against: the peripheral
    # configuration and every finding's answer are the documented basis, so an
    # application call outside it is flagged as an assumption, not a citation.
    cubemx_text = _cubemx_context(plan)
    findings_text = "\n".join(finding.answer for finding in hardware.findings)

    steps = architecture.implementation_order
    if not steps:
        warnings.append("No implementation steps in architecture; falling back to file tree.")
        files = architecture.file_tree or ["Core/Src/main.c"]
        steps = [
            ImplementationStep(
                order=1,
                title="Application Implementation",
                detail="Implement application logic",
                files=files,
                citations=architecture.citations,
            )
        ]

    for step in steps:
        step_files = step.files
        if not step_files:
            continue

        evidence_text = _step_evidence(step, hardware)

        for file_path in step_files:
            logger.info(
                "Generating firmware file %s (step %d: %s)",
                file_path,
                step.order,
                step.title,
            )

            scaffold_content = ""
            if file_path in ("Core/Src/main.c", "Core/Inc/main.h"):
                if project_id and workspace.exists(project_id):
                    try:
                        scaffold_content = workspace.read_file(project_id, file_path)
                    except Exception:
                        scaffold_content = _scaffold_fallback(file_path, plan)
                else:
                    scaffold_content = _scaffold_fallback(file_path, plan)

            user_prompt = build_file_prompt(
                file_path=file_path,
                step=step,
                requirements=requirements,
                hardware=hardware,
                architecture=architecture,
                plan=plan,
                generated_files=generated_files,
                scaffold_content=scaffold_content,
            )

            messages = [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]

            try:
                proposal, repair_warnings, _ = await request_contract(
                    llm, FileProposal, messages, temperature=0.0
                )
                warnings.extend(repair_warnings)
            except ContractError as exc:
                logger.error("Failed to generate file %s: %s", file_path, exc)
                warnings.append(f"Failed to generate {file_path}: {exc}")
                continue

            final_contents = proposal.contents
            if file_path in ("Core/Src/main.c", "Core/Inc/main.h") and scaffold_content:
                final_contents = _merge_scaffold_file(
                    file_path, proposal.contents, scaffold_content
                )

            valid_citations: list[str] = []
            for citation in proposal.citations:
                if citation in known_citations:
                    valid_citations.append(citation)
                    if citation not in citations:
                        citations.append(citation)
                else:
                    warnings.append(f"{file_path}: dropped unverifiable citation {citation!r}")
                    assumptions.append(f"{file_path}: unverified claim referencing {citation}")

            if proposal.assumptions:
                assumptions.extend(proposal.assumptions)

            support_text = "\n".join(
                [
                    cubemx_text,
                    evidence_text,
                    scaffold_content,
                    findings_text,
                    " ".join(valid_citations),
                ]
            )
            undocumented = _undocumented_apis(final_contents, support_text)
            if undocumented:
                listed = undocumented[:_MAX_LISTED_APIS]
                suffix = (
                    f" (and {len(undocumented) - _MAX_LISTED_APIS} more)"
                    if len(undocumented) > _MAX_LISTED_APIS
                    else ""
                )
                assumptions.append(
                    f"{file_path}: uses HAL APIs with no documented evidence: "
                    f"{', '.join(listed)}{suffix}"
                )

            source_file = SourceFile(
                path=file_path,
                purpose=proposal.purpose or step.title,
                contents=final_contents,
                step_order=step.order,
                citations=valid_citations,
                generated=True,
            )
            generated_files.append(source_file)

    # A path that appears in two steps would otherwise land in the bundle twice
    # -- a misleading file count and a duplicate on disk. Keep the last version
    # generated for each path and say so, so the collision is visible.
    if len({f.path for f in generated_files}) != len(generated_files):
        deduped: dict[str, SourceFile] = {}
        for source_file in generated_files:
            if source_file.path in deduped:
                warnings.append(
                    f"{source_file.path}: targeted by more than one step; "
                    "kept the last generated version"
                )
            deduped[source_file.path] = source_file
        generated_files = list(deduped.values())

    notes = [f"Generated {len(generated_files)} files across {len(steps)} steps."]
    if hardware.coverage < _LOW_COVERAGE:
        unverified = (
            f"unverified: hardware coverage {hardware.coverage:.0%} is low; "
            "generated code may lack documentary support"
        )
        warnings.append(unverified)
        notes.append(unverified)

    bundle = FirmwareBundle(
        files=generated_files,
        notes=notes,
        assumptions=list(dict.fromkeys(assumptions)),
        warnings=list(dict.fromkeys(warnings)),
        citations=citations,
        evidence=architecture.evidence,
    )

    if project_id and workspace.exists(project_id):
        workspace.write_files(project_id, bundle.files)
        try:
            scaffold_project(
                project_id,
                plan,
                clean=False,
                target=project_name or project_id,
                summary=requirements.summary,
            )
        except Exception as exc:
            logger.warning("Scaffold refresh failed after writing firmware files: %s", exc)
            warnings.append(f"Scaffold refresh warning: {exc}")

    return bundle, warnings


async def firmware_node(state: dict[str, Any]) -> dict[str, Any]:
    """LangGraph node for the Firmware Agent.

    Entered twice in a run when the build failed: the second time (the build
    node set `repair_next`) it patches the existing files instead of
    regenerating them, and bumps `attempt` for the next build.
    """
    if state.get("repair_next"):
        return await _repair_node(state)

    requirements = parse_stored(Requirements, state.get("requirements"))
    hardware = parse_stored(HardwareFindings, state.get("hardware"))
    architecture = parse_stored(Architecture, state.get("architecture"))
    plan = parse_stored(CubeMXPlan, state.get("cubemx"))

    project_id = state.get("project_id", "")
    project_name = state.get("project_name", "") or project_id

    bundle, warnings = await generate_firmware(
        requirements,
        hardware,
        architecture,
        plan,
        project_id=project_id,
        project_name=project_name,
    )

    return {
        "firmware": dump(bundle),
        "firmware_artifacts": {
            "file_count": len(bundle.files),
            "files": bundle.paths,
            "warnings": warnings,
        },
        "attempt": 1,
        "repair_next": False,
    }


def _repair_context(plan: CubeMXPlan, bundle: FirmwareBundle) -> str:
    """Hardware config plus every project header's declarations.

    The repair model sees excerpts of one file only. Without the headers it
    guesses at what the other files offer -- the first real run "fixed" a
    missing `MPU6050_Select` by calling an equally non-existent
    `spi_bus_select`, citing a header it had never been shown.
    """
    return "\n".join(
        [
            _cubemx_context(plan),
            "\n# Project headers (the only project functions and types that exist)",
            _headers_context(bundle.files),
        ]
    )


async def _repair_node(state: dict[str, Any]) -> dict[str, Any]:
    bundle = parse_stored(FirmwareBundle, state.get("firmware"))
    result = parse_stored(BuildResult, state.get("build"))
    plan = parse_stored(CubeMXPlan, state.get("cubemx"))
    attempt = int(state.get("attempt") or 1) + 1

    bundle, warnings, report = await repair_firmware(
        bundle,
        result,
        project_id=state.get("project_id", ""),
        context=_repair_context(plan, bundle),
    )
    return {
        "firmware": dump(bundle),
        "firmware_artifacts": {
            "file_count": len(bundle.files),
            "files": bundle.paths,
            "warnings": warnings,
            "repair": report,
        },
        "attempt": attempt,
        "repair_next": False,
    }
