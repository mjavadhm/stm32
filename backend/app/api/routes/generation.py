from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session

from app.db.models import GenerationSetting, PinSelectionPolicy, utcnow
from app.db.session import get_session

router = APIRouter(prefix="/generation", tags=["generation"])


class GenerationSettingUpdate(BaseModel):
    pin_selection_policy: PinSelectionPolicy


def effective_generation_settings(session: Session) -> GenerationSetting:
    row = session.get(GenerationSetting, 1)
    return row or GenerationSetting()


@router.get("/settings")
def get_generation_settings(session: Session = Depends(get_session)) -> dict:
    row = effective_generation_settings(session)
    return {"pin_selection_policy": row.pin_selection_policy}


@router.put("/settings")
def update_generation_settings(
    payload: GenerationSettingUpdate,
    session: Session = Depends(get_session),
) -> dict:
    row = session.get(GenerationSetting, 1) or GenerationSetting()
    row.pin_selection_policy = payload.pin_selection_policy.value
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return {"pin_selection_policy": row.pin_selection_policy}
