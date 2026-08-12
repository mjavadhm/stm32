"""Shared plumbing for agents that must return a structured contract.

`docs/architecture.md` decision #3 is "JSON Schema + Pydantic validation +
retry", but only the first two thirds were implemented: the first malformed
reply went straight to a degraded run. Feeding the parser error back and
asking once more recovers most of them, and it matters more from M4 on --
longer JSON payloads are exactly where local models drift out of schema.

The LLM is duck-typed (anything with `async chat(messages, **kwargs)`), so
agents stay testable with a fake and this module never imports a provider.
"""

import logging
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from app.core.config import settings
from app.orchestrator.contracts import ContractError, parse_model

logger = logging.getLogger(__name__)

TContract = TypeVar("TContract", bound=BaseModel)


class ChatLLM(Protocol):
    async def chat(self, messages: list[dict], **kwargs: Any) -> str: ...


_REPAIR_PROMPT = (
    "Your previous reply could not be parsed: {error}\n\n"
    "Reply again with ONLY the JSON object described in the system message. "
    "No prose, no markdown fence, no trailing commas, all strings quoted."
)

# The rejected reply is echoed back so the model can see its own mistake, but
# a truncated copy: a 30k-character runaway would otherwise be re-sent in full.
_ECHO_LIMIT = 2000


async def request_contract(
    llm: ChatLLM,
    model: type[TContract],
    messages: list[dict],
    *,
    temperature: float = 0.0,
    retries: int | None = None,
    **chat_kwargs: Any,
) -> tuple[TContract, list[str], str]:
    """Ask for a contract, repairing a malformed reply before giving up.

    Returns `(contract, warnings, raw_reply)`. Raises `ContractError` when
    every attempt fails, leaving the degrade-or-fail decision to the agent.
    """
    attempts = 1 + (settings.llm_contract_retries if retries is None else retries)
    warnings: list[str] = []
    conversation = list(messages)
    reply = ""
    last_error: ContractError | None = None

    for attempt in range(1, attempts + 1):
        reply = await llm.chat(conversation, temperature=temperature, **chat_kwargs)
        try:
            return parse_model(model, reply), warnings, reply
        except ContractError as exc:
            last_error = exc
            logger.warning(
                "%s: invalid reply on attempt %d/%d: %s",
                model.__name__,
                attempt,
                attempts,
                exc,
            )
            if attempt == attempts:
                break
            warnings.append(f"{model.__name__}: reply was repaired after {exc}")
            conversation = [
                *messages,
                {"role": "assistant", "content": reply[:_ECHO_LIMIT]},
                {"role": "user", "content": _REPAIR_PROMPT.format(error=exc)},
            ]

    raise last_error or ContractError("no reply from the model")
