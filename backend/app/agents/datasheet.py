"""Datasheet Agent -- the first agent that answers from retrieved sources.

This is the reference implementation every later agent should copy:

  1. retrieve from PageVault *before* prompting the model
  2. answer strictly from that context, with citations
  3. degrade instead of failing when the knowledge base is unavailable

It is written as a coroutine and is intended to run as a LangGraph node in
M3, which is why the worker drives the graph with `astream`.
"""

import logging
import re
from typing import Any

from app.core.llm import get_agent_llm
from app.rag import RagContext, get_rag_client

logger = logging.getLogger(__name__)

AGENT_NAME = "datasheet"

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

_FAMILY_RE = re.compile(r"\bSTM32[A-Z]\d?", re.IGNORECASE)


def detect_family(text: str) -> str | None:
    """Pull a chip family (STM32F4, STM32H7 ...) out of free text.

    Used to narrow retrieval: an F4 question must not be answered from an F1
    reference manual, and the two describe genuinely different peripherals.
    """
    match = _FAMILY_RE.search(text or "")
    return match.group(0).upper() if match else None


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

    return {
        "agent": AGENT_NAME,
        "question": question,
        "family": family,
        "answer": answer,
        "citations": context.citations(),
        "identifiers": context.identifiers,
        "sources_used": {
            "symbols": len(context.symbols),
            "types": len(context.type_context),
            "chunks": len(context.chunks),
            "pages": len(context.pages),
        },
        "grounded": context.available and not context.is_empty,
        "warnings": context.warnings,
    }


async def datasheet_node(state: dict[str, Any]) -> dict[str, Any]:
    """LangGraph node wrapper (wired into the graph in M3)."""
    question = state.get("user_request", "")
    result = await answer_hardware_question(question)
    return {"datasheet": result}
