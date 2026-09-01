"""Knowledge-base endpoints (PageVault).

These exist so retrieval can be exercised and debugged on its own, without
running a whole pipeline -- which is what you want while tuning chunking,
collections and filters. The collections/documents listings also power the
chat UI's scope selectors ("ask against this collection, or only this
document").
"""

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.agents.datasheet import answer_hardware_question, detect_family
from app.core.config import settings
from app.rag import get_rag_client

router = APIRouter(prefix="/rag", tags=["rag"])


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    family: str | None = None
    text_top_k: int | None = Field(default=None, ge=1, le=50)
    symbol_top_k: int | None = Field(default=None, ge=0, le=50)
    page_top_k: int | None = Field(default=None, ge=0, le=20)
    # Scope overrides: which part of the knowledge base to search.
    text_collection: str | None = None
    page_collection: str | None = None
    document_ids: list[str] | None = Field(default=None, max_length=20)


class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    family: str | None = None


@router.get("/health")
async def rag_health() -> dict:
    """Is the knowledge base reachable? Never raises."""
    reachable = await get_rag_client().health()
    return {
        "enabled": settings.rag_enabled,
        "url": settings.pagevault_url,
        "reachable": reachable,
        "text_collection": settings.rag_text_collection,
        "page_collection": settings.rag_page_collection,
    }


@router.post("/search")
async def rag_search(payload: SearchRequest) -> dict:
    """Raw retrieval, no LLM. Use this to judge whether recall is the problem."""
    context = await get_rag_client().search(
        payload.query,
        family=payload.family or detect_family(payload.query),
        text_top_k=payload.text_top_k,
        symbol_top_k=payload.symbol_top_k,
        page_top_k=payload.page_top_k,
        text_collection=payload.text_collection,
        page_collection=payload.page_collection,
        document_ids=payload.document_ids,
    )
    return {
        "query": context.query,
        "available": context.available,
        "identifiers": context.identifiers,
        "citations": context.citations(),
        "symbols": [
            {
                "name": s.name,
                "signature": s.signature,
                "citation": s.citation,
                "match": s.metadata.get("match", ""),
            }
            for s in context.symbols
        ],
        "types": [
            {"name": s.name, "signature": s.signature, "citation": s.citation}
            for s in context.type_context
        ],
        "chunks": [
            {
                "name": s.name,
                "citation": s.citation,
                "score": s.score,
                "text": s.text[:600],
            }
            for s in context.chunks
        ],
        "pages": [
            {"citation": s.citation, "score": s.score, "image_url": s.metadata.get("image_url")}
            for s in context.pages
        ],
        "warnings": context.warnings,
    }


@router.post("/ask")
async def rag_ask(payload: AskRequest) -> dict:
    """Datasheet Agent: retrieval + a cited answer."""
    return await answer_hardware_question(payload.question, family=payload.family)


@router.get("/collections")
async def rag_collections() -> dict:
    """What can the user choose to ask against? Never raises."""
    rag = get_rag_client()
    text, text_warning = await rag.list_text_collections()
    visual, visual_warning = await rag.list_visual_collections()
    warnings = [w for w in (text_warning, visual_warning) if w]
    return {
        "available": not warnings,
        # The configured defaults, so the UI can label its "default" option.
        "default_text_collection": settings.rag_text_collection,
        "default_page_collection": settings.rag_page_collection,
        "text": text,
        "visual": visual,
        "warnings": warnings,
    }


@router.get("/documents")
async def rag_documents(
    collection: str = Query(min_length=1),
    kind: str = Query(default="text", pattern="^(text|visual)$"),
) -> dict:
    """Documents ("parts") of one collection, for the chat UI's scope selector."""
    documents, warning = await get_rag_client().list_documents(
        collection, visual=(kind == "visual")
    )
    documents.sort(key=lambda d: d.get("path", ""))
    return {
        "available": warning is None,
        "collection": collection,
        "kind": kind,
        "documents": documents,
        "warning": warning,
    }
