"""Requirements Agent (M3) -- free text in, structured requirements out.

The user writes one sentence in Persian or English. Everything downstream
(datasheet lookups, architecture, code generation) needs that sentence turned
into explicit fields, so this agent is where ambiguity gets *named* instead of
quietly resolved.

Two behaviours matter more than the prompt:

* **Never invent a part number.** An unstated MCU becomes an assumption or an
  open question, never a plausible-looking guess that M4 then designs around.
* **Never block the pipeline.** A malformed reply degrades to a minimal
  requirements object carrying the raw request, because a design run that
  continues with warnings is more useful than one that dies at node two.
"""

import logging
from typing import Any

from app.agents.base import request_contract
from app.agents.datasheet import detect_family
from app.core.llm import get_agent_llm
from app.orchestrator.contracts import (
    ContractError,
    PeripheralNeed,
    Requirements,
    dump,
)

logger = logging.getLogger(__name__)

AGENT_NAME = "requirements"

_SYSTEM_PROMPT = """You are a requirements analyst for STM32 embedded firmware projects.

Turn the user's request into a structured requirements document.
The request may be written in Persian or English; always answer in English.

Rules:
- Extract only what the user stated or what is unambiguously implied.
- NEVER invent a part number, pin, clock speed or peripheral instance. If the
  user did not specify it, either record it under "assumptions" (when there is
  one obvious default) or under "open_questions" (when a real choice exists).
- "family" is the chip family such as STM32F4, derived from "mcu" when given.
- "transfer_mode" is one of: polling, interrupt, dma, or "" when unspecified.
- "rtos" is "none" unless the user asked for an RTOS.
- "deliverables" is what the user expects to receive (firmware source, tests,
  documentation, CubeMX configuration ...).

Reply with ONLY a JSON object, no commentary, in exactly this shape:
{
  "summary": "one sentence",
  "mcu": "STM32F407VG",
  "family": "STM32F4",
  "board": "",
  "peripherals": [
    {"name": "SPI1", "role": "read MPU6050", "protocol": "SPI", "transfer_mode": "dma"}
  ],
  "external_components": ["MPU6050"],
  "rtos": "none",
  "constraints": [],
  "deliverables": [],
  "assumptions": [],
  "open_questions": []
}"""


def _fallback(user_request: str, reason: str) -> tuple[Requirements, str]:
    """Minimal requirements so the pipeline can continue after a bad reply."""
    return Requirements(
        summary=user_request.strip()[:300],
        family=detect_family(user_request) or "",
        assumptions=[],
        open_questions=[
            "Structured requirements could not be extracted automatically; "
            "the raw request needs manual review."
        ],
        constraints=[],
        deliverables=[],
        peripherals=[],
    ), reason


def _normalise(requirements: Requirements, user_request: str) -> Requirements:
    """Fill in what can be derived locally, and keep enum-ish fields clean.

    Done in code rather than trusted to the model: the family is recoverable
    from the MCU string with a regex, and every later stage filters retrieval
    on it, so a blank family silently costs the run all of its citations.
    """
    family = requirements.family or detect_family(requirements.mcu) or detect_family(
        user_request
    )
    peripherals = [
        PeripheralNeed(
            name=item.name.strip(),
            role=item.role.strip(),
            protocol=item.protocol.strip().upper(),
            transfer_mode=item.transfer_mode.strip().lower(),
        )
        for item in requirements.peripherals
        if item.name.strip()
    ]
    return requirements.model_copy(
        update={
            "family": (family or "").upper(),
            "rtos": (requirements.rtos or "none").strip().lower(),
            "peripherals": peripherals,
        }
    )


async def analyze_requirements(user_request: str) -> tuple[Requirements, list[str]]:
    """Return structured requirements plus any warnings raised on the way."""
    warnings: list[str] = []
    llm = get_agent_llm(AGENT_NAME)
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_request},
    ]

    try:
        requirements, repair_warnings, _reply = await request_contract(
            llm, Requirements, messages
        )
    except ContractError as exc:
        logger.warning("requirements parsing failed: %s", exc)
        requirements, reason = _fallback(user_request, str(exc))
        warnings.append(f"requirements degraded: {reason}")
        return requirements, warnings

    warnings.extend(repair_warnings)

    requirements = _normalise(requirements, user_request)
    if not requirements.mcu:
        warnings.append("No MCU part number was specified.")
    if not requirements.family:
        warnings.append(
            "No chip family could be determined; documentation retrieval "
            "will not be filtered and may return results for other families."
        )
    return requirements, warnings


async def requirements_node(state: dict[str, Any]) -> dict[str, Any]:
    """LangGraph node."""
    requirements, warnings = await analyze_requirements(state.get("user_request", ""))
    return {"requirements": {**dump(requirements), "warnings": warnings}}
