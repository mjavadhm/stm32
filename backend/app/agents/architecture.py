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

from app.agents.base import request_contract
from app.core.llm import get_agent_llm
from app.orchestrator.contracts import (
    Architecture,
    ContractError,
    HardwareFindings,
    ImplementationStep,
    Requirements,
    dump,
    parse_stored,
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
- Use a reference only under the topic it was retrieved for: the sources listed
  under "SPI1" support SPI1 decisions, not USART2 ones.
- Every implementation step lists the files it creates or edits in "files", and
  the references that justify it in "citations". Code generation prompts with
  exactly those sources, so a step without them is a step written from memory.
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
     "responsibility": "...", "depends_on": ["spi_bus"], "citations": []}
  ],
  "peripherals": [
    {"peripheral": "SPI1", "mode": "master, full-duplex, 8-bit",
     "transfer_mode": "dma", "dma_stream": "", "pins": [], "clock_hint": "",
     "rationale": "...", "citation": ""}
  ],
  "file_tree": ["Core/Src/main.c"],
  "implementation_order": [
    {"order": 1, "title": "...", "detail": "...", "modules": ["spi_bus"],
     "files": ["Core/Src/spi_bus.c"], "citations": []}
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
    # exclude_defaults keeps empty fields and schema_version out of the
    # prompt: tokens spent restating "board": "" are tokens not spent on
    # evidence, and a local model reads shorter prompts more faithfully.
    sections = [
        "# Requirements",
        requirements.model_dump_json(indent=2, exclude_defaults=True),
    ]

    if hardware.findings:
        sections.append("\n# Documentation findings (cite these)")
        for finding in hardware.findings:
            refs = ", ".join(finding.citations) if finding.citations else "no source"
            sections.append(
                f"\n## {finding.topic}\n"
                f"Allowed references for {finding.topic}: {refs}\n\n"
                f"{finding.answer}"
            )
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


def _allowed_citations(hardware: HardwareFindings, name: str) -> set[str]:
    """Sources retrieved for *this* peripheral, not for the run as a whole.

    With one retrieval per peripheral, a global allow-list lets a reference
    pulled for USART2 be pasted onto an SPI1 decision and still pass.
    """
    key = name.strip().lower()
    if not key:
        return set()
    exact = hardware.citations_for(key)
    if exact:
        return set(exact)
    related: set[str] = set()
    for finding in hardware.findings:
        topic = finding.topic.strip().lower()
        if topic and (topic in key or key in topic):
            related.update(finding.citations)
    return related


def _infer_step_citations(
    step: ImplementationStep,
    evidence: dict[str, list[str]],
    limit: int = 4,
) -> list[str]:
    """Attach the evidence a step needs without asking the model twice.

    A step that names SPI1 gets the sources retrieved for SPI1. Deterministic
    on purpose: M4 prompts with these, so they must not be a second guess.
    """
    haystack = " ".join(
        [step.title, step.detail, *step.modules, *step.files]
    ).lower()
    inferred: list[str] = []
    for topic, citations in evidence.items():
        if topic.strip().lower() in haystack:
            for citation in citations:
                if citation not in inferred:
                    inferred.append(citation)
    return inferred[:limit]


def _enforce_evidence(
    architecture: Architecture,
    hardware: HardwareFindings,
) -> tuple[Architecture, list[str]]:
    """Keep only citations that really came back from retrieval.

    A model asked to cite sources will happily produce a reference with the
    right shape and the wrong content. Anything not in the retrieved set is
    dropped and the decision is recorded as an assumption instead; anything
    borrowed from another topic is kept but flagged.
    """
    known = set(hardware.citations)
    evidence = hardware.evidence_map()
    warnings: list[str] = []
    peripherals = []
    assumptions = list(architecture.assumptions)

    for plan in architecture.peripherals:
        citation = plan.citation.strip()
        if citation and citation not in known:
            logger.info("dropping unverifiable citation %r", citation)
            warnings.append(
                f"{plan.peripheral}: unverifiable citation dropped ({citation})"
            )
            assumptions.append(
                f"{plan.peripheral}: {plan.rationale or 'configuration'} "
                f"(no supporting source in the knowledge base)"
            )
            plan = plan.model_copy(update={"citation": ""})
        elif citation and citation not in _allowed_citations(hardware, plan.peripheral):
            # A real source retrieved for another question. Kept, because
            # shared files (HAL DMA, the clock tree) legitimately cover
            # several peripherals -- flagged, because borrowing a reference
            # is also what a model does when it wants one and has none.
            warnings.append(
                f"{plan.peripheral}: cites {citation}, retrieved for another topic"
            )
        peripherals.append(plan)

    modules = [
        module.model_copy(
            update={"citations": [c for c in module.citations if c in known]}
        )
        for module in architecture.modules
    ]

    steps = []
    for index, step in enumerate(architecture.implementation_order, start=1):
        citations = [c for c in step.citations if c in known]
        if not citations:
            citations = _infer_step_citations(step, evidence)
        steps.append(step.model_copy(update={"order": index, "citations": citations}))

    used = [plan.citation for plan in peripherals if plan.citation]
    citations = [c for c in hardware.citations if c in used]

    return (
        architecture.model_copy(
            update={
                "peripherals": peripherals,
                "modules": modules,
                "implementation_order": steps,
                "assumptions": assumptions,
                "citations": citations,
                "evidence": evidence,
                "rtos": architecture.rtos or "none",
            }
        ),
        warnings,
    )


async def design_architecture(
    requirements: Requirements,
    hardware: HardwareFindings,
) -> tuple[Architecture, list[str]]:
    warnings: list[str] = []
    llm = get_agent_llm(AGENT_NAME)
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(requirements, hardware)},
    ]

    try:
        architecture, repair_warnings, _reply = await request_contract(
            llm, Architecture, messages
        )
    except ContractError as exc:
        logger.warning("architecture parsing failed: %s", exc)
        return (
            Architecture(
                overview="Architecture could not be generated automatically.",
                risks=[f"Design stage failed: {exc}"],
            ),
            [f"architecture degraded: {exc}"],
        )

    warnings.extend(repair_warnings)
    architecture, evidence_warnings = _enforce_evidence(architecture, hardware)
    warnings.extend(evidence_warnings)

    if not hardware.grounded:
        warnings.append(
            "Designed without documentation sources; every hardware claim is "
            "unverified."
        )
    elif not hardware.verified:
        # Sources existed and no answer used them: the design reads grounded
        # while resting on model memory.
        warnings.append(
            "Sources were retrieved but no hardware answer referenced them; "
            "treat the design as unverified."
        )
    if not architecture.implementation_order:
        warnings.append("No implementation order was produced.")
    return architecture, warnings


async def architecture_node(state: dict[str, Any]) -> dict[str, Any]:
    """LangGraph node. Reads the contracts written by the previous nodes."""
    requirements = parse_stored(Requirements, state.get("requirements"))
    hardware = parse_stored(HardwareFindings, state.get("hardware"))
    architecture, warnings = await design_architecture(requirements, hardware)
    return {"architecture": {**dump(architecture), "warnings": warnings}}
