"""OpenAI-compatible LLM client factory.

Model resolution order for each agent:
  1. `agent_settings` row in the database (editable from the dashboard later)
  2. default `LLM_MODEL` from .env

Switching provider (online API -> Ollama) = changing `.env` only.

Usage inside an agent:
    llm = get_agent_llm("firmware")
    text = await llm.chat([{"role": "user", "content": "..."}])
"""

import logging
import re
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import lru_cache

from openai import AsyncOpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_llm_client() -> AsyncOpenAI:
    """Shared provider client.

    Cached deliberately: every `AsyncOpenAI()` carries its own httpx
    connection pool, so building one per call meant a new pool (and new TCP
    handshakes) for every single agent step.
    """
    return AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key or "not-set",
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )


@lru_cache(maxsize=1)
def get_embedding_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key or "not-set",
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )


def reset_clients() -> None:
    """Drop cached clients. Only needed by tests that patch settings."""
    get_llm_client.cache_clear()
    get_embedding_client.cache_clear()


async def aclose_llm_clients() -> None:
    """Close the cached provider clients and drop them from the cache.

    These clients are process-wide singletons holding an httpx connection
    pool, but a Celery worker runs every task in a fresh ``asyncio.run()``
    loop. A pool opened inside task #1's loop is dead by the time task #2
    starts ("Event loop is closed"), so the worker closes them per task and
    lets the next one build its own.
    """
    for getter in (get_llm_client, get_embedding_client):
        if not getter.cache_info().currsize:
            continue
        try:
            await getter().close()
        except Exception:  # pragma: no cover - best-effort cleanup
            logger.debug("closing provider client failed", exc_info=True)
    reset_clients()


# --------------------------------------------------------------------------
# agent_settings cache
#
# resolve_agent_model() and is_agent_enabled() are called from async agent
# code. A synchronous DB round-trip there blocks the event loop, and it used
# to happen on every single LLM call. Read the whole table at once and keep
# it for a few seconds; the settings API invalidates on write, so the TTL
# only covers changes made directly in the database.
# --------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: dict[str, tuple[str | None, bool]] | None = None
_cache_loaded_at: float = 0.0


def invalidate_agent_settings_cache() -> None:
    """Called by the settings API after a write."""
    global _cache, _cache_loaded_at
    with _cache_lock:
        _cache = None
        _cache_loaded_at = 0.0


def _load_agent_settings() -> dict[str, tuple[str | None, bool]]:
    """Read every agent_settings row as {name: (model, enabled)}."""
    from sqlmodel import Session, select

    from app.db.models import AgentSetting
    from app.db.session import engine

    with Session(engine) as session:
        rows = session.exec(select(AgentSetting)).all()
        return {row.agent_name: (row.model, row.enabled) for row in rows}


def _agent_settings() -> dict[str, tuple[str | None, bool]]:
    global _cache, _cache_loaded_at
    now = time.monotonic()
    with _cache_lock:
        fresh = (
            _cache is not None
            and (now - _cache_loaded_at) < settings.agent_settings_cache_ttl
        )
        if fresh:
            return _cache  # type: ignore[return-value]
    try:
        loaded = _load_agent_settings()
    except Exception:
        # DB unavailable (unit tests, early startup) -> behave as if no
        # overrides exist. Do not cache a failure.
        return {}
    with _cache_lock:
        _cache = loaded
        _cache_loaded_at = time.monotonic()
    return loaded


def resolve_agent_model(agent_name: str) -> str:
    """DB override (agent_settings) -> default LLM_MODEL."""
    model, _enabled = _agent_settings().get(agent_name, (None, True))
    return model or settings.llm_model


def is_agent_enabled(agent_name: str) -> bool:
    """Check the agent's enabled flag in agent_settings (default: enabled)."""
    _model, enabled = _agent_settings().get(agent_name, (None, True))
    return enabled


@dataclass(frozen=True)
class AgentLLM:
    """LLM handle for one agent: shared provider client + that agent's model."""

    agent_name: str
    client: AsyncOpenAI
    model: str

    async def chat(self, messages: list[dict], **kwargs) -> str:
        if settings.llm_max_tokens and "max_tokens" not in kwargs:
            kwargs["max_tokens"] = settings.llm_max_tokens
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            **kwargs,
        )
        return resp.choices[0].message.content or ""

    # Models that reason before answering (glm-5.x, o1/o3, DeepSeek-R ...).
    # Matched against the model name so the check costs nothing per call.
    _REASONING_RE = re.compile(
        settings.llm_reasoning_model_pattern, re.IGNORECASE
    )

    async def plan(self, messages: list[dict], **kwargs) -> str:
        """A `chat` call for pure JSON decision steps (query planning).

        Reasoning models burn their latency *before* the first output
        token, and a JSON action gains nothing from it: measured 63s with
        full reasoning vs 20s at minimal effort for the same 3-token reply.
        So on reasoning models, planning runs at minimal effort. Providers
        that reject the parameter answer 400, which is caught and retried
        once without it.
        """
        if self._REASONING_RE.search(self.model or ""):
            kwargs.setdefault("reasoning_effort", settings.llm_planning_reasoning_effort)
            try:
                return await self.chat(messages, **kwargs)
            except Exception as exc:
                if "reasoning_effort" not in str(exc):
                    raise
                logger.warning(
                    "%s: provider rejected reasoning_effort; retrying without it",
                    self.agent_name,
                )
                kwargs.pop("reasoning_effort", None)
        return await self.chat(messages, **kwargs)

    async def stream(self, messages: list[dict], **kwargs) -> AsyncIterator[str]:
        """Yield the reply token by token.

        Only the final answer of a chat turn is streamed; planning steps
        stay on chat() so a malformed JSON action never reaches the UI as
        half-rendered text.

        Reasoning models (glm-4.7+, o1-style) spend completion tokens on
        hidden reasoning before any visible content, so max_tokens must
        never clip a stream: what looks like "10 tokens is plenty for a
        short answer" silently produces an empty one. The setting only
        applies to non-streaming calls, where an empty reply is at least
        visible as a failure.
        """
        kwargs.pop("max_tokens", None)
        if settings.llm_max_tokens and settings.llm_stream_max_tokens:
            kwargs.setdefault("max_tokens", settings.llm_stream_max_tokens)
        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            **kwargs,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta


def get_agent_llm(agent_name: str) -> AgentLLM:
    """Build the LLM handle for an agent (e.g. "router", "firmware")."""
    return AgentLLM(
        agent_name=agent_name,
        client=get_llm_client(),
        model=resolve_agent_model(agent_name),
    )


async def llm_healthcheck() -> dict:
    """Send a tiny prompt to verify provider connectivity (M0 acceptance check)."""
    client = get_llm_client()
    resp = await client.chat.completions.create(
        model=settings.llm_model,
        messages=[{"role": "user", "content": "Reply with the single word: pong"}],
        max_tokens=5,
    )
    return {
        "model": settings.llm_model,
        "base_url": settings.llm_base_url,
        "reply": resp.choices[0].message.content,
    }
