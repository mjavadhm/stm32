from fastapi import APIRouter, HTTPException

from app.core.config import settings
from app.core.llm import llm_healthcheck

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
async def health() -> dict:
    return {
        "status": "ok",
        "app": settings.app_name,
        "environment": settings.environment,
    }


@router.get("/llm")
async def health_llm() -> dict:
    """M0 acceptance criterion: configured LLM provider answers a test request."""
    try:
        result = await llm_healthcheck()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503, detail=f"LLM provider unreachable: {exc}"
        ) from exc
    return {"status": "ok", **result}
