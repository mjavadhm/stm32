"""Architecture Agent (M3) -- requirements + hardware facts in, design out.

This is the last node before code generation, so its output is the closest
thing the project has to a spec. Two properties are enforced in code rather
than hoped for in the prompt:

* **Evidence is traceable.** A peripheral decision that cites a retrieved
  source keeps that citation. One that does not is demoted to an assumption,
  so nobody downstream mistakes a fluent guess for a datasheet fact.
* **The plan is walkable.** `implementation_order` is renumbered here, because
  M4 iterates it step by step and duplicate or missing indices break that loop.
"""

import logging
from typing import Any

from app.core.llm import get_agent_llm
from app.orchestrator.contracts import (
    Architecture,
    ContractError,
    HardwareFindings,
    Requirements,
    dump,
    parse_model,
)

logger = logging.getLogger(__name__)

AGENT_NAME = "architecture"

_SYSTEM_PROMPT = """You are a firmware architect for STM32 microcontrollers.

Design the firmware architecture for the given requirements. Do NOT write code:
produce the structure, the peripheral decisions and the build order that a
firmware engineer would follow.

Rules:
- Choose HAL, LL, or a mix, and say why in "overview".
- For every peripheral choose polling, interrupt or DMA, and justify it in
  "rationale". High-rate sensor streaming justifies DMA; a debug console does not.
- When a hardware fact comes from the provided documentation excerpts, copy its
  bracketed reference into "citation" for that peripheral, e.g.
  "stm32f4xx_hal_spi.c:120-180". If you have no source for a claim, leave
  "citation" empty and state the claim under "assumptions" instead.
- Never invent pin numbers or DMA streams. Leave them empty rather than guessing.
- "implementation_order" must be buildable step by step: each step should
  compile and be testable before the next one starts.
- "file_tree" lists the files to be created, as paths.

Reply with ONLY a JSON object, no commentary, in exactly this shape:
{
  "overview": "two or three sentences",
  "driver_layer": "hal",
  "rtos": "none",
  "modules": [
    {"name": "mpu6050", "path": "Core/Src/mpu6050.c", "layer": "driver",
     "responsibility": "...", "depends_on": ["spi_bus"]}
  ],
  "peripherals": [
    {"peripheral": "SPI1", "mode": "master, full-duplex, 8-bit",
     "transfer_mode": "dma", "dma_stream": "", "pins": [], "clock_hint": "",
     "rationale": "...", "citation": ""}
  ],
  "file_tree": ["Core/Src/main.c"],
  "implementation_order": [
    {"order": 1, "title": "...", "detail": "...", "modules": ["spi_bus"]}
  ],
  "risks": [],
  "assumptions": []
}"""


def build_user_prompt(
    requirements: Requirements,
    hardware: HardwareFindings,
) -> str:
    """Requirements first, then evidence, then the explicit gaps.

    Open questions are repeated at the end on purpose: the architect must
    design *around* an unknown rather than silently picking a value for it.
    """
    sections = ["# Requirements", requirements.model_dump_json(indent=2)]

    if hardware.findings:
        sections.append("\n# Documentation findings (cite these)")
        for finding in hardware.findings:
            refs = ", ".join(finding.citations) if finding.citations else "no source"
            sections.append(f"\n## {finding.topic}  [{refs}]\n{finding.answer}")
    else:
        sections.append(
            "\n# Documentation findings\n"
            "None available. Leave every \"citation\" empty and record the "
            "hardware claims you rely on under \"assumptions\"."
        )

    if requirements.open_questions:
        sections.append(
            "\n# Unresolved questions\n"
            + "\n".join(f"- {question}" for question in requirements.open_questions)
            + "\nDesign so these can be answered later; do not invent answers."
        )
    return "\n".join(sections)


def _enforce_evidence(
    architecture: Architecture,
    hardware: HardwareFindings,
) -> Architecture:
    """Keep only citations that really came back from retrieval.

    A model asked to cite sources will happily produce a reference with the
    right shape and the wrong content. Anything not in the retrieved set is
    dropped and the decision is recorded as an assumption instead.
    """
    known = set(hardware.citations)
    peripherals = []
    assumptions = list(architecture.assumptions)

    for plan in architecture.peripherals:
        citation = plan.citation.strip()
        if citation and citation not in known:
            logger.info("dropping unverifiable citation %r", citation)
            assumptions.append(
                f"{plan.peripheral}: {plan.rationale or 'configuration'} "
                f"(no supporting source in the knowledge base)"
            )
            plan = plan.model_copy(update={"citation": ""})
        peripherals.append(plan)

    steps = [
        step.model_copy(update={"order": index})
        for index, step in enumerate(architecture.implementation_order, start=1)
    ]

    used = [plan.citation for plan in peripherals if plan.citation]
    citations = [c for c in hardware.citations if c in used] or used

    return architecture.model_copy(
        update={
            "peripherals": peripherals,
            "implementation_order": steps,
            "assumptions": assumptions,
            "citations": citations,
            "rtos": architecture.rtos or "none",
        }
    )


async def design_architecture(
    requirements: Requirements,
    hardware: HardwareFindings,
) -> tuple[Architecture, list[str]]:
    warnings: list[str] = []
    llm = get_agent_llm(AGENT_NAME)
    reply = await llm.chat(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(requirements, hardware)},
        ],
        temperature=0,
    )

    try:
        architecture = parse_model(Architecture, reply)
    except ContractError as exc:
        logger.warning("architecture parsing failed: %s", exc)
        return (
            Architecture(
                overview="Architecture could not be generated automatically.",
                risks=[f"Design stage failed: {exc}"],
            ),
            [f"architecture degraded: {exc}"],
        )

    architecture = _enforce_evidence(architecture, hardware)
    if not hardware.grounded:
        warnings.append(
            "Designed without documentation sources; every hardware claim is "
            "unverified."
        )
    if not architecture.implementation_order:
        warnings.append("No implementation order was produced.")
    return architecture, warnings


async def architecture_node(state: dict[str, Any]) -> dict[str, Any]:
    """LangGraph node. Reads the contracts written by the previous nodes."""
    requirements = Requirements.model_validate(state.get("requirements") or {})
    hardware = HardwareFindings.model_validate(state.get("hardware") or {})
    architecture, warnings = await design_architecture(requirements, hardware)
    return {"architecture": {**dump(architecture), "warnings": warnings}}
