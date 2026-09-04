"""Chat Agent -- agentic RAG over the knowledge base.

Unlike the Datasheet Agent (one retrieve-then-answer pass), this agent
*plans its own retrieval*: the model is asked, before every search, whether
it wants one and with which query, sees a compact summary of what came
back, and may search again until it has enough evidence -- bounded by
`chat_max_searches`. When it is ready, the accumulated contexts are
rendered into a single prompt and the answer is streamed token by token.

Design rules (inherited from the Datasheet Agent, `app/rag/client.py`):

  * never raise: KB down, provider down or a model that cannot speak the
    action protocol all degrade to an answer with warnings
  * cite sources inline using the exact bracketed references
  * the decision protocol is JSON-over-text (see `request_contract`),
    not native function calling -- local models behind Ollama do not
    reliably support tools, and the repair machinery already exists

Events (yielded by `answer_with_search`, mapped 1:1 to SSE by the API):

  * {"type": "search", ...}          a retrieval was planned and started
  * {"type": "search_result", ...}   what that retrieval brought back
  * {"type": "delta", ...}           one answer token
  * {"type": "done", ...}            final answer + metadata
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agents.base import request_contract
from app.agents.datasheet import detect_family
from app.core.config import settings
from app.core.llm import get_agent_llm
from app.orchestrator.contracts import ContractError
from app.rag import RagContext, get_rag_client

logger = logging.getLogger(__name__)

AGENT_NAME = "chat"

# History is trimmed to this many (role, content) pairs, user side included.
# Follow-up questions need the turns that establish topic and family, but a
# 50-turn transcript would crowd the context the retrieval evidence needs.
MAX_HISTORY_MESSAGES = 10


class _SearchAction(BaseModel):
    """The planner's reply: run one more search, or answer now."""

    action: Literal["search", "ready"]
    # Ignored (and may be empty) when action == "ready".
    query: str = Field(default="", max_length=500)


_DECISION_SYSTEM_PROMPT = """You are the retrieval planner of a documentation
assistant for STM32 microcontrollers. Your job is to decide which queries to
run against the knowledge base (ST reference manuals, datasheets, HAL/LL
sources, vendor examples) before the question is answered.

Reply with ONLY a JSON object, no prose, no markdown fence:
- {"action": "search", "query": "..."} to run one more search
- {"action": "ready"} when you have planned enough searches

Planning rules:
- One focused topic per query. A query that has to cover SPI, DMA and a
  sensor at once wastes the top-k on whichever topic dominates.
- Prefer concrete identifiers (HAL function names, register names, part
  numbers) over vague descriptions; symbol search matches those exactly.
- Search again only when the question spans topics the previous query did
  not cover, or the evidence so far is clearly insufficient.
- Do not plan more searches than the question needs: two or three well
  aimed queries answer almost anything; more is noise."""


_ANSWER_SYSTEM_PROMPT = """You are a hardware documentation specialist for STM32
microcontrollers, answering a user's question in a chat.

Answer ONLY from the provided context, which comes from ST reference manuals,
datasheets, HAL/LL sources and vendor examples.

Rules:
- Cite the source of every technical claim inline, copying the bracketed
  reference EXACTLY as it appears above the excerpt, including the path and
  the line numbers: [hal-mini/Src/stm32f4xx_hal_spi.c:1643-1743].
- A citation is a whole-token copy. Never shorten the path, never invent line
  numbers, never write a bare file name -- the UI turns these references into
  links back to the source, so a mangled one is a dead link.
- Every paragraph that states a fact carries at least one such reference.
- Register names, bit fields, addresses and function signatures must be
  copied from the context verbatim. Never reconstruct them from memory.
- If the context does not contain the answer, say exactly what is missing and
  what document would answer it. Do not guess.
- Be concise and technical. No preamble.
- Reply in the same language the user wrote in."""


_NO_CONTEXT_PROMPT = """You are a hardware documentation specialist for STM32
microcontrollers, answering a user's question in a chat.

The documentation knowledge base is currently unavailable, so you have no
retrieved sources. Answer from general knowledge, but:
- Begin with: "Warning: answered without documentation sources."
- Explicitly flag every register name, bit field or address as unverified.
- Recommend the specific ST document the user should check.
- Reply in the same language the user wrote in."""


def _trim_history(
    history: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    """Keep the most recent turns, newest last, roles normalised."""
    if not history:
        return []
    trimmed: list[dict[str, str]] = []
    for item in history[-MAX_HISTORY_MESSAGES:]:
        role = str(item.get("role") or "").lower()
        content = str(item.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            trimmed.append({"role": role, "content": content})
    return trimmed


def _summarize_results(query: str, context: RagContext) -> str:
    """Compact feedback for the planner: what the search brought back.

    The planner does not need the excerpt text -- it needs to know *which*
    sources exist so it can judge coverage. Full text goes only to the
    answer phase, once.
    """
    if not context.available:
        return f'Search "{query}" failed: the knowledge base is unavailable.'
    if context.is_empty:
        return f'Search "{query}" returned nothing.'

    lines: list[str] = []
    for snippet in [*context.symbols, *context.type_context]:
        label = snippet.signature or snippet.name
        lines.append(f"- {snippet.citation} {label}")
    for snippet in context.chunks:
        lines.append(f"- {snippet.citation} {snippet.name}")
    for snippet in context.pages:
        lines.append(f"- {snippet.citation} (page image)")
    return f'Search "{query}" returned:\n' + "\n".join(lines)


def _build_answer_messages(
    question: str,
    history: list[dict[str, str]],
    contexts: list[RagContext],
) -> list[dict[str, str]]:
    grounded = [c for c in contexts if c.available and not c.is_empty]
    if grounded:
        # Several searches share one character ceiling, so the budget is
        # split evenly instead of letting the first context eat it all.
        per_context = max(1000, settings.rag_context_max_chars // len(grounded))
        blocks = "\n\n".join(c.as_prompt(per_context) for c in grounded)
        system = _ANSWER_SYSTEM_PROMPT
        user = f"# Retrieved context\n\n{blocks}\n\n# Question\n\n{question}"
    else:
        system = _NO_CONTEXT_PROMPT
        user = question

    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    messages.extend(history)
    messages.append({"role": "user", "content": user})
    return messages


async def answer_with_search(
    question: str,
    *,
    history: list[dict[str, str]] | None = None,
    family: str | None = None,
    text_collection: str | None = None,
    page_collection: str | None = None,
    document_ids: list[str] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Answer one chat turn, planning retrieval along the way.

    The optional scope narrows where the agent may search: a text/page
    collection, or specific documents within them (the chat UI's
    collection and "part" selectors).

    Yields the event stream described in the module docstring. Never
    raises: every failure mode degrades to a done event with warnings.
    """
    history = _trim_history(history)
    family = family or detect_family(question)
    if family is None:
        # Follow-ups ("that timer") carry no family of their own; resolve
        # one from the turns that established the topic.
        for item in reversed(history):
            family = detect_family(item["content"])
            if family:
                break

    scope = {
        "text_collection": text_collection,
        "page_collection": page_collection,
        "document_ids": document_ids,
    }

    async def _run_search(query: str) -> RagContext:
        return await rag.search(
            query,
            family=family,
            text_collection=text_collection,
            page_collection=page_collection,
            document_ids=document_ids,
        )

    llm = get_agent_llm(AGENT_NAME)
    rag = get_rag_client()
    warnings: list[str] = []
    contexts: list[RagContext] = []
    searches: list[str] = []
    max_searches = max(1, settings.chat_max_searches)
    planning_failed = False

    # ---- decision loop: plan searches until "ready" or the cap ----
    decision: list[dict[str, str]] = [
        {"role": "system", "content": _DECISION_SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": question},
    ]
    while len(searches) < max_searches:
        try:
            action, _plan_warnings, _raw = await request_contract(
                llm, _SearchAction, decision, temperature=0, call="plan"
            )
        except ContractError as exc:
            warnings.append(f"retrieval planning failed: {exc}")
            planning_failed = True
            break
        except Exception as exc:
            # Provider outage (auth, network, rate limit): planning is not
            # the user's problem to read as a crash -- degrade like every
            # other failure mode and let the answer phase try the no-context
            # prompt.
            logger.warning("chat planning call failed: %s", exc)
            warnings.append(f"retrieval planning call failed: {exc}")
            planning_failed = True
            break

        if action.action != "search" or not action.query.strip():
            break

        query = action.query.strip()
        searches.append(query)
        yield {"type": "search", "query": query, "index": len(searches), "max": max_searches}

        context = await _run_search(query)
        contexts.append(context)
        yield {
            "type": "search_result",
            "query": query,
            "available": context.available,
            "citations": context.citations(),
            "sources": {
                "symbols": len(context.symbols),
                "types": len(context.type_context),
                "chunks": len(context.chunks),
                "pages": len(context.pages),
            },
            "warnings": list(context.warnings),
        }
        warnings.extend(context.warnings)

        if not context.available:
            # The knowledge base is down; further searches fail identically.
            break

        decision = [
            *decision,
            {"role": "assistant", "content": json.dumps({"action": "search", "query": query})},
            {"role": "user", "content": _summarize_results(query, context)},
        ]

    if planning_failed and not searches:
        # Fall back to the retrieve-before-prompting doctrine: even without
        # a planner, one direct search with the raw question beats nothing.
        query = question
        searches.append(query)
        yield {"type": "search", "query": query, "index": 1, "max": max_searches}
        context = await _run_search(query)
        contexts.append(context)
        yield {
            "type": "search_result",
            "query": query,
            "available": context.available,
            "citations": context.citations(),
            "sources": {
                "symbols": len(context.symbols),
                "types": len(context.type_context),
                "chunks": len(context.chunks),
                "pages": len(context.pages),
            },
            "warnings": list(context.warnings),
        }
        warnings.extend(context.warnings)
    elif not searches:
        # The planner judged the question answerable without retrieval. It
        # is still answered grounded: one search with the raw question.
        query = question
        searches.append(query)
        yield {"type": "search", "query": query, "index": 1, "max": max_searches}
        context = await _run_search(query)
        contexts.append(context)
        yield {
            "type": "search_result",
            "query": query,
            "available": context.available,
            "citations": context.citations(),
            "sources": {
                "symbols": len(context.symbols),
                "types": len(context.type_context),
                "chunks": len(context.chunks),
                "pages": len(context.pages),
            },
            "warnings": list(context.warnings),
        }
        warnings.extend(context.warnings)

    # ---- answer phase: stream the cited answer ----
    answer_messages = _build_answer_messages(question, history, contexts)
    answer = ""
    failed = False
    try:
        async for delta in llm.stream(answer_messages, temperature=0):
            answer += delta
            yield {"type": "delta", "text": delta}
    except Exception as exc:
        logger.warning("chat answer streaming failed: %s", exc)
        detail = str(exc) or f"{type(exc).__module__}.{type(exc).__name__}"
        warnings.append(f"answer generation failed: {detail}")
        failed = True

    citations: list[str] = []
    for context in contexts:
        for citation in context.citations():
            if citation not in citations:
                citations.append(citation)
    cited = [citation for citation in citations if citation in answer]
    grounded = any(c.available and not c.is_empty for c in contexts)
    if grounded and not cited:
        warnings.append("sources were retrieved but the answer cited none of them")

    yield {
        "type": "done",
        "answer": answer,
        "citations": citations,
        "cited": cited,
        "grounded": grounded,
        "verified": bool(cited),
        "searches": searches,
        "scope": scope,
        "warnings": warnings,
        "failed": failed,
    }
