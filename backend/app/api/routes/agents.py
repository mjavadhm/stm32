from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.agents import KNOWN_AGENTS
from app.core.config import settings
from app.db.models import AgentSetting, utcnow
from app.db.session import get_session

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("/settings")
def list_agent_settings(session: Session = Depends(get_session)) -> list[dict]:
    """Effective settings per agent. The dashboard Agents page (M7) reads this."""
    rows = {r.agent_name: r for r in session.exec(select(AgentSetting)).all()}
    result = []
    for name in KNOWN_AGENTS:
        row = rows.get(name)
        result.append(
            {
                "agent_name": name,
                "model": (row.model if row and row.model else settings.llm_model),
                "is_override": bool(row and row.model),
                "enabled": row.enabled if row else True,
            }
        )
    return result


class AgentSettingUpdate(BaseModel):
    # model = "" or null clears the override (falls back to LLM_MODEL)
    model: str | None = None
    enabled: bool | None = None


@router.put("/settings/{agent_name}")
def update_agent_setting(
    agent_name: str,
    payload: AgentSettingUpdate,
    session: Session = Depends(get_session),
) -> dict:
    if agent_name not in KNOWN_AGENTS:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_name}")

    row = session.get(AgentSetting, agent_name)
    if row is None:
        row = AgentSetting(agent_name=agent_name)

    if "model" in payload.model_fields_set:
        row.model = payload.model or None
    if payload.enabled is not None:
        row.enabled = payload.enabled
    row.updated_at = utcnow()

    session.add(row)
    session.commit()
    session.refresh(row)
    return {
        "agent_name": row.agent_name,
        "model": row.model or settings.llm_model,
        "is_override": bool(row.model),
        "enabled": row.enabled,
    }
