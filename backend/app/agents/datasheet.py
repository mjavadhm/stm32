"""Datasheet Agent -- the first agent that answers from retrieved sources.

This is the reference implementation every later agent should copy:

  1. retrieve from PageVault *before* prompting the model
  2. answer strictly from that context, with citations
  3. degrade instead of failing when the knowledge base is unavailable

Two entry points:

* `answer_hardware_question()` -- one question, used by `POST /rag/ask` so
  retrieval can be exercised without running a pipeline.
* `gather_hardware_findings()` -- the M3 graph node: it takes the peripherals
  and external components from `Requirements`, asks one focused question per
  item concurrently, and returns a `HardwareFindings` contract for the
  Architecture Agent. With an empty knowledge base the findings come back
  ungrounded and the run continues -- that is the designed degradation.
"""

import asyncio
import logging
import re
from typing import Any

from app.core.llm import get_agent_llm
from app.orchestrator.contracts import (
    HardwareFinding,
    HardwareFindings,
    Requirements,
    dump,
    parse_stored,
)
from app.rag import RagContext, get_rag_client

logger = logging.getLogger(__name__)

AGENT_NAME = "datasheet"

# Retrieval + generation per peripheral, run concurrently. Bounded because a
# local model behind Ollama serialises requests anyway, and an unbounded fan-out
# would just queue them while holding open connections.
MAX_PARALLEL_QUESTIONS = 4

_SYSTEM_PROMPT = """You are a hardware documentation specialist for STM32 microcontrollers.

Answer ONLY from the provided context, which comes from ST reference manuals,
datasheets, HAL/LL sources and vendor examples.

Rules:
- Cite the source of every technical claim inline, using the exact bracketed
  reference shown above the excerpt, e.g. [stm32f4xx_hal_spi.c:120-180].
- Register names, bit fields, addresses and function signatures must be
  copied from the context verbatim. Never reconstruct them from memory.
- If the context does not contain the answer, say exactly what is missing and
  what document would answer it. Do not guess.
- Be concise and technical. No preamble."""

_NO_CONTEXT_PROMPT = """You are a hardware documentation specialist for STM32 microcontrollers.

The documentation knowledge base is currently unavailable, so you have no
retrieved sources. Answer from general knowledge, but:
- Begin with: "Warning: answered without documentation sources."
- Explicitly flag every register name, bit field or address as unverified.
- Recommend the specific ST document the user should check."""

# A part number always carries a digit after the family letters (STM32F407,
# STM32WB55, STM32MP157). Requiring that digit keeps "STM32CubeMX" from being
# read as the "STM32C" family, and accepting two letters keeps WB / WL / MP
# from being truncated to "STM32W" / "STM32M".
_FAMILY_RE = re.compile(r"\bSTM32(?P<letters>[A-Z]{1,2})(?P<digit>\d)", re.IGNORECASE)

# Families whose name keeps both letters.
_TWO_LETTER_FAMILIES = {"WB", "WL", "MP"}


def detect_family(text: str) -> str | None:
    """Pull a chip family (STM32F4, STM32WB, STM32MP1 ...) out of free text.

    Used to narrow retrieval: an F4 question must not be answered from an F1
    reference manual, and the two describe genuinely different peripherals.
    """
    match = _FAMILY_RE.search(text or "")
    if not match:
        return None
    letters = match.group("letters").upper()
    digit = match.group("digit")
    if letters in _TWO_LETTER_FAMILIES:
        # STM32WB55 -> STM32WB, but STM32MP157 -> STM32MP1.
        return f"STM32{letters}{digit if letters == 'MP' else ''}"
    return f"STM32{letters[0]}{digit}"


def build_messages(question: str, context: RagContext) -> list[dict[str, str]]:
    if not context.available or context.is_empty:
        return [
            {"role": "system", "content": _NO_CONTEXT_PROMPT},
            {"role": "user", "content": question},
        ]
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"# Retrieved context\n\n{context.as_prompt()}\n\n"
                f"# Question\n\n{question}"
            ),
        },
    ]


async def answer_hardware_question(
    question: str,
    *,
    family: str | None = None,
) -> dict[str, Any]:
    """Retrieve, then answer with citations.

    Returns a JSON-serialisable dict so it can be stored directly in
    `TaskRun.result`.
    """
    family = family or detect_family(question)
    context = await get_rag_client().search(question, family=family)

    llm = get_agent_llm(AGENT_NAME)
    answer = await llm.chat(build_messages(question, context), temperature=0)

    citations = context.citations()
    # `grounded` only says the shelf had books on it. `cited` says the model
    # opened one: the reference has to appear in the answer it wrote.
    cited = [citation for citation in citations if citation in answer]

    return {
        "agent": AGENT_NAME,
        "question": question,
        "family": family,
        "answer": answer,
        "citations": citations,
        "cited": cited,
        "identifiers": context.identifiers,
        "sources_used": {
            "symbols": len(context.symbols),
            "types": len(context.type_context),
            "chunks": len(context.chunks),
            "pages": len(context.pages),
        },
        "grounded": context.available and not context.is_empty,
        "verified": bool(cited),
        "warnings": context.warnings,
    }


# --------------------------------------------------------------------------
# M3: one focused question per peripheral, instead of one vague question per
# project. Retrieval quality collapses when a single query has to cover SPI,
# DMA and a sensor at once -- the top-k is spent on whichever topic dominates.
# --------------------------------------------------------------------------


def build_questions(requirements: Requirements) -> list[tuple[str, str]]:
    """Turn requirements into (topic, question) pairs.

    Peripherals first, then external components, because the architecture
    agent reads the findings in order and peripheral decisions are the ones it
    has to justify.
    """
    target = requirements.mcu or requirements.family or "the target STM32 MCU"
    questions: list[tuple[str, str]] = []
    seen: set[str] = set()

    for peripheral in requirements.peripherals:
        name = peripheral.name.strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)

        mode = peripheral.transfer_mode.strip().lower()
        if mode == "dma":
            transfer = " with DMA"
        elif mode in ("interrupt", "polling"):
            transfer = f" in {mode} mode"
        else:
            transfer = ""
        purpose = f" (used to {peripheral.role.strip()})" if peripheral.role.strip() else ""

        questions.append(
            (
                name,
                f"On {target}, how do I configure and use {name}{transfer}{purpose}? "
                "Give the required HAL or LL functions, the registers and bit "
                "fields involved, the clock and pin constraints, and the "
                "stream or channel mapping when a transfer engine is used.",
            )
        )

    for component in requirements.external_components:
        name = component.strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        questions.append(
            (
                name,
                f"How is the {name} interfaced to {target}? "
                "Give the bus and wiring, the initialisation sequence, the "
                "register map needed to read measurements, and any timing or "
                "electrical constraint that affects the driver.",
            )
        )

    return questions


async def gather_hardware_findings(
    requirements: Requirements,
    *,
    max_parallel: int = MAX_PARALLEL_QUESTIONS,
) -> HardwareFindings:
    """Answer one question per peripheral/component and collect the evidence.

    Never raises: a question that fails becomes an ungrounded finding with the
    error recorded in `warnings`, because the architecture agent is expected to
    design around missing evidence rather than the run stopping here.
    """
    family = (
        requirements.family
        or detect_family(requirements.mcu)
        or detect_family(requirements.summary)
        or ""
    )
    questions = build_questions(requirements)
    if not questions:
        return HardwareFindings(
            family=family,
            warnings=[
                "No peripherals or external components were identified, so no "
                "documentation was retrieved."
            ],
        )

    semaphore = asyncio.Semaphore(max(1, max_parallel))

    async def ask(topic: str, question: str) -> tuple[str, str, dict[str, Any]]:
        async with semaphore:
            try:
                result = await answer_hardware_question(question, family=family or None)
            except Exception as exc:  # provider down, unexpected reply shape ...
                logger.warning("datasheet question failed for %s: %s", topic, exc)
                result = {
                    "answer": "",
                    "citations": [],
                    "cited": [],
                    "grounded": False,
                    "warnings": [f"question failed: {exc}"],
                }
            return topic, question, result

    answers = await asyncio.gather(*(ask(topic, q) for topic, q in questions))

    findings: list[HardwareFinding] = []
    warnings: list[str] = []
    for topic, question, result in answers:
        findings.append(
            HardwareFinding(
                topic=topic,
                question=question,
                answer=result.get("answer", ""),
                citations=list(result.get("citations") or []),
                cited=list(result.get("cited") or []),
                grounded=bool(result.get("grounded")),
            )
        )
        if result.get("grounded") and not result.get("cited"):
            # Retrieval worked and the model ignored it. Silent until now,
            # and the single most common way a "grounded" answer is wrong.
            warnings.append(
                f"{topic}: sources were retrieved but the answer cited none of them"
            )
        for warning in result.get("warnings") or []:
            message = f"{topic}: {warning}"
            if message not in warnings:
                warnings.append(message)

    if findings and not any(finding.grounded for finding in findings):
        warnings.append(
            "No documentation sources were retrieved; every hardware answer is "
            "unverified."
        )

    return HardwareFindings(family=family, findings=findings, warnings=warnings)


async def datasheet_node(state: dict[str, Any]) -> dict[str, Any]:
    """LangGraph node: `requirements` in, `hardware` out.

    The state key is `hardware` because that is what the architecture agent
    reads; writing anything else here silently costs the design every citation
    it could have had.
    """
    requirements = parse_stored(Requirements, state.get("requirements"))

    if not requirements.peripherals and not requirements.external_components:
        # Requirements degraded (or the request never named a peripheral):
        # fall back to the raw request so the node still contributes evidence.
        question = state.get("user_request", "") or requirements.summary
        result = await answer_hardware_question(
            question, family=requirements.family or None
        )
        findings = HardwareFindings(
            family=result.get("family") or requirements.family or "",
            findings=[
                HardwareFinding(
                    topic="general",
                    question=question,
                    answer=result["answer"],
                    citations=result["citations"],
                    cited=result["cited"],
                    grounded=result["grounded"],
                )
            ],
            warnings=[
                "No structured peripheral list was available; asked one "
                "general question instead.",
                *result["warnings"],
            ],
        )
    else:
        findings = await gather_hardware_findings(requirements)

    return {"hardware": dump(findings)}
