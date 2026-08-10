"""Request router (M1 — LLM-powered).

Detects the pipeline entry point from the raw user request:
full project generation (End-to-End) vs Copilot modes (debug/optimize/test).

Primary: LLM classification via get_agent_llm("router") — the router agent's
model comes from agent_settings (DB) with fallback to LLM_MODEL.
Fallback: deterministic keyword rules — used when the router agent is
disabled, the provider is unreachable, or the reply is malformed.
"""

import asyncio
import logging
from typing import Any

from app.core.config import settings
from app.core.llm import get_agent_llm, is_agent_enabled
from app.db.models import RequestType

logger = logging.getLogger(__name__)

_RULES: list[tuple[RequestType, tuple[str, ...]]] = [
    (
        RequestType.debug,
        ("hardfault", "hard fault", "debug", "crash", "fault", "خطا", "ارور", "دیباگ"),
    ),
    (
        RequestType.optimize,
        ("optimi", "بهینه", "مصرف حافظه", "performance", "footprint"),
    ),
    (
        RequestType.test,
        ("unit test", "unittest", "تست"),
    ),
]

_LABELS = {member.value: member for member in RequestType}

_SYSTEM_PROMPT = (
    "You are a request router for an STM32 embedded firmware engineering "
    "assistant. Classify the user's request into exactly one category:\n"
    "- full_project: generate a new project/firmware from requirements\n"
    "- debug: analyze compile/linker/runtime errors (e.g. HardFault) in existing code\n"
    "- optimize: reduce RAM/Flash usage or CPU cycles of existing code\n"
    "- test: write or run unit/integration tests for existing code\n"
    "The request may be in Persian or English. "
    "Reply with ONLY the category name, nothing else."
)


def classify_by_rules(text: str) -> RequestType:
    """Deterministic keyword classification (fallback path)."""
    lowered = text.lower()
    for request_type, keywords in _RULES:
        if any(keyword in lowered for keyword in keywords):
            return request_type
    return RequestType.full_project


async def _classify_with_llm(text: str) -> RequestType:
    llm = get_agent_llm("router")
    reply = await llm.chat(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        max_tokens=10,
        temperature=0,
    )
    label = reply.strip().lower().strip("\"'`.")
    if label in _LABELS:
        return _LABELS[label]
    raise ValueError(f"Unexpected router reply: {reply!r}")


async def classify_request(text: str) -> tuple[RequestType, str]:
    """LLM classification with keyword-rule fallback. Returns (type, method).

    Bounded by `router_timeout_seconds`: a slightly worse label is much better
    than a pipeline stuck on an unresponsive provider. The ceiling is separate
    from the client's own timeout because it also has to cover the SDK's
    internal retries.

    The method ("llm" or "rules") is returned rather than logged away: when a
    request is routed to the wrong pipeline, the first thing worth knowing is
    whether the model chose it or the keyword fallback did.
    """
    if not is_agent_enabled("router"):
        return classify_by_rules(text), "rules:disabled"
    try:
        request_type = await asyncio.wait_for(
            _classify_with_llm(text),
            timeout=settings.router_timeout_seconds,
        )
        return request_type, "llm"
    except TimeoutError:
        return classify_by_rules(text), "rules:timeout"
    except Exception as exc:
        # Provider unreachable or malformed reply -> deterministic fallback.
        logger.warning("router LLM classification failed: %s", exc)
        return classify_by_rules(text), "rules:error"


async def router_node(state: dict[str, Any]) -> dict[str, Any]:
    """LangGraph node: the pipeline's first step (M3).

    Routing moved out of the `POST /projects` handler so the API can return as
    soon as the project row exists, and so the routing decision shows up in
    the progress view like any other agent instead of happening invisibly.
    """
    request_type, method = await classify_request(state.get("user_request", ""))
    return {
        "request_type": request_type.value,
        "routing": {"request_type": request_type.value, "method": method},
    }
