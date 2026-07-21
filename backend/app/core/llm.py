"""OpenAI-compatible LLM client factory.

Model resolution order for each agent:
  1. `agent_settings` row in the database (editable from the dashboard later)
  2. default `LLM_MODEL` from .env

Switching provider (online API -> Ollama) = changing `.env` only.

Usage inside an agent:
    llm = get_agent_llm("firmware")
    text = await llm.chat([{"role": "user", "content": "..."}])
"""

from dataclasses import dataclass

from openai import AsyncOpenAI

from app.core.config import settings


def get_llm_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key or "not-set",
    )


def get_embedding_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key or "not-set",
    )


def resolve_agent_model(agent_name: str) -> str:
    """DB override (agent_settings) -> default LLM_MODEL."""
    try:
        from sqlmodel import Session

        from app.db.models import AgentSetting
        from app.db.session import engine

        with Session(engine) as session:
            row = session.get(AgentSetting, agent_name)
            if row is not None and row.model:
                return row.model
    except Exception:
        # DB unavailable (unit tests, early startup) -> fall back silently.
        pass
    return settings.llm_model


def is_agent_enabled(agent_name: str) -> bool:
    """Check the agent's enabled flag in agent_settings (default: enabled)."""
    try:
        from sqlmodel import Session

        from app.db.models import AgentSetting
        from app.db.session import engine

        with Session(engine) as session:
            row = session.get(AgentSetting, agent_name)
            return row.enabled if row is not None else True
    except Exception:
        return True


@dataclass(frozen=True)
class AgentLLM:
    """LLM handle for one agent: shared provider client + that agent's model."""

    agent_name: str
    client: AsyncOpenAI
    model: str

    async def chat(self, messages: list[dict], **kwargs) -> str:
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            **kwargs,
        )
        return resp.choices[0].message.content or ""


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
